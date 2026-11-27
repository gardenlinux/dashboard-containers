#!/usr/bin/env python3
"""
Collect metrics from gardenlinux container-* repos and write dashboard-data.json.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from packaging.version import Version, InvalidVersion

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ORG = "gardenlinux"
OUTPUT_FILE = "dashboard-data.json"
RELEASE_BRANCH_RE = re.compile(r"^release/v(.+)$")
BASE_IMAGE_RE = re.compile(r'BASE_IMAGE="\$\{BASE_IMAGE:-[^"]+/(fips):([^"]+)\}"')

session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


def gh_get(path: str, params: dict = None):
    url = f"https://api.github.com{path}"
    for attempt in range(3):
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            print(f"  Rate limited / server error on {path}, sleeping {retry_after}s", file=sys.stderr)
            time.sleep(retry_after)
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        time.sleep(0.1)
        return resp.json()
    raise RuntimeError(f"Failed after 3 attempts: {url}")


def get_all_pages(path: str, params: dict = None) -> list:
    params = dict(params or {})
    params.setdefault("per_page", 100)
    results = []
    page = 1
    while True:
        params["page"] = page
        data = gh_get(path, params)
        if not data:
            break
        results.extend(data)
        if len(data) < params["per_page"]:
            break
        page += 1
    return results


def get_container_repos() -> list:
    repos = get_all_pages(f"/orgs/{ORG}/repos", {"type": "public"})
    names = sorted(
        r["name"] for r in repos
        if r["name"].startswith("container-") and not r.get("archived", False)
    )
    print(f"Found {len(names)} container repos: {', '.join(names)}", file=sys.stderr)
    return names


def get_latest_release(repo: str):
    data = gh_get(f"/repos/{ORG}/{repo}/releases/latest")
    if data is None:
        return None
    return {
        "version": data["tag_name"],
        "url": data["html_url"],
        "published_at": data["published_at"],
    }


def get_base_image_tag(repo: str):
    app_name = repo.removeprefix("container-")
    script = f"build-{app_name}-fips.sh"
    url = f"https://raw.githubusercontent.com/{ORG}/{repo}/main/{script}"
    resp = session.get(url, timeout=30)
    if resp.status_code != 200:
        return None
    match = BASE_IMAGE_RE.search(resp.text)
    return f"{match.group(1)}:{match.group(2)}" if match else None


def _rollup_conclusion(workflow_runs: list) -> str:
    statuses = [r["status"] for r in workflow_runs]
    conclusions = [r["conclusion"] for r in workflow_runs if r["status"] == "completed"]

    if any(s in ("in_progress", "queued", "waiting", "requested") for s in statuses):
        return "pending"

    failure_conclusions = {"failure", "startup_failure", "timed_out"}
    if any(c in failure_conclusions for c in conclusions):
        return "failure"
    if any(c == "action_required" for c in conclusions):
        return "approval_required"

    if conclusions and all(c == "success" for c in conclusions):
        return "success"

    return "none"


def get_pr_ci_status(repo: str, head_sha: str) -> str:
    runs = gh_get(
        f"/repos/{ORG}/{repo}/actions/runs",
        {"head_sha": head_sha, "event": "pull_request", "per_page": 20},
    )
    if not runs or not runs.get("workflow_runs"):
        return "none"
    return _rollup_conclusion(runs["workflow_runs"])


def get_check_upstream_status(repo: str):
    data = gh_get(
        f"/repos/{ORG}/{repo}/actions/workflows/check-upstream.yml/runs",
        {"per_page": 1, "branch": "main"},
    )
    if not data or not data.get("workflow_runs"):
        return None
    run = data["workflow_runs"][0]
    conclusion = run.get("conclusion")
    status = run.get("status")
    if status in ("in_progress", "queued", "waiting", "requested"):
        rollup = "pending"
    elif conclusion == "action_required":
        rollup = "approval_required"
    elif conclusion in ("failure", "startup_failure", "timed_out"):
        rollup = "failure"
    elif conclusion == "success":
        rollup = "success"
    else:
        rollup = "none"
    return {
        "status": rollup,
        "url": run["html_url"],
        "run_at": run["created_at"],
    }


def get_pending_update(repo: str, current_version: str):
    # Step 1+2: find all release/v* branches strictly newer than current release
    branches = get_all_pages(f"/repos/{ORG}/{repo}/branches")
    candidates = []
    try:
        current_v = Version(current_version.lstrip("v"))
    except InvalidVersion:
        return None

    for b in branches:
        m = RELEASE_BRANCH_RE.match(b["name"])
        if not m:
            continue
        try:
            if Version(m.group(1)) > current_v:
                candidates.append(m.group(1))
        except InvalidVersion:
            continue

    if not candidates:
        return None

    # Step 3: select the newest among candidates
    newest = str(max(candidates, key=Version))
    branch = f"release/v{newest}"
    create_pr_url = (
        f"https://github.com/{ORG}/{repo}/compare/main...{branch}?expand=1"
    )

    # Step 4: check if an open PR already targets this branch
    prs = gh_get(
        f"/repos/{ORG}/{repo}/pulls",
        {"state": "open", "head": f"{ORG}:{branch}", "per_page": 5},
    )
    if prs:
        pr = prs[0]
        head_sha = pr["head"]["sha"]
        return {
            "new_version": newest,
            "create_pr_url": create_pr_url,
            "pr_number": pr["number"],
            "pr_url": pr["html_url"],
            "head_sha": head_sha,
            "ci_status": get_pr_ci_status(repo, head_sha),
        }

    return {
        "new_version": newest,
        "create_pr_url": create_pr_url,
    }


def main():
    repo_names = get_container_repos()
    repos_data = []

    for name in repo_names:
        print(f"Processing {name}...", file=sys.stderr)
        latest_release = get_latest_release(name)
        base_image_tag = get_base_image_tag(name)
        check_upstream = get_check_upstream_status(name)

        if latest_release:
            pending_update = get_pending_update(name, latest_release["version"])
        else:
            pending_update = None

        repos_data.append({
            "name": name,
            "full_name": f"{ORG}/{name}",
            "url": f"https://github.com/{ORG}/{name}",
            "latest_release": latest_release,
            "base_image_tag": base_image_tag,
            "pending_update": pending_update,
            "check_upstream": check_upstream,
        })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repos": repos_data,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote {OUTPUT_FILE} with {len(repos_data)} repos", file=sys.stderr)


if __name__ == "__main__":
    main()
