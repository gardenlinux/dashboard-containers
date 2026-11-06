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

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
ORG = "gardenlinux"
OUTPUT_FILE = "dashboard-data.json"
UPDATE_PR_TITLE_RE = re.compile(r"^Release v(.+)$")
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
    # Build script is always named build-{appname}-fips.sh
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

    failure_conclusions = {"failure", "startup_failure", "action_required", "timed_out"}
    if any(c in failure_conclusions for c in conclusions):
        return "failure"

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
    elif conclusion in ("failure", "startup_failure", "action_required", "timed_out"):
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


def get_pending_update(repo: str):
    prs = gh_get(
        f"/repos/{ORG}/{repo}/pulls",
        {"state": "open", "per_page": 20, "sort": "created", "direction": "desc"},
    )
    if not prs:
        return None

    for pr in prs:
        match = UPDATE_PR_TITLE_RE.match(pr["title"])
        if not match:
            continue

        new_version = match.group(1)
        head_sha = pr["head"]["sha"]
        ci_status = get_pr_ci_status(repo, head_sha)

        return {
            "pr_number": pr["number"],
            "pr_title": pr["title"],
            "pr_url": pr["html_url"],
            "new_version": new_version,
            "head_sha": head_sha,
            "ci_status": ci_status,
        }

    return None


def main():
    repo_names = get_container_repos()
    repos_data = []

    for name in repo_names:
        print(f"Processing {name}...", file=sys.stderr)
        latest_release = get_latest_release(name)
        base_image_tag = get_base_image_tag(name)
        pending_update = get_pending_update(name)
        check_upstream = get_check_upstream_status(name)

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
