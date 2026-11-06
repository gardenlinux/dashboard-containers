.PHONY: collect serve open test

PORT ?= 8080

collect:
	GITHUB_TOKEN=$${GITHUB_TOKEN:?GITHUB_TOKEN is not set} python3 scripts/collect_data.py

serve:
	@echo "Serving dashboard at http://localhost:$(PORT)"
	python3 -m http.server $(PORT) --directory .

test: collect serve

open:
	xdg-open http://localhost:$(PORT) 2>/dev/null || open http://localhost:$(PORT)
