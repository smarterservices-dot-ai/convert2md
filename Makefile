# convert2md — make help lists everything.

.DEFAULT_GOAL := help
.PHONY: help install check clean

install: ## One-shot setup: Python deps + crawl4ai post-install (Chromium).
	uv sync
	uv run crawl4ai-setup
	@echo
	@echo "✔ convert2md installed."
	@echo "  CLI:        uv run python -m convert2md --help"
	@echo "  Extension:  load extension/ unpacked in chrome://extensions"

check: ## Format check, lint, type-check, all tests; auto-syncs prompts to extension.
	@# Mirror canonical prompts → extension; remove any stale ones first.
	@rm -f extension/prompts/*.md
	@cp convert2md/prompts/*.md extension/prompts/
	uv run ruff format --check convert2md tests
	uv run ruff check convert2md tests
	uv run mypy convert2md
	uv run pytest -q
	npm run lint --prefix extension
	npm test --prefix extension

clean: ## Remove caches and build artifacts.
	rm -rf dist build *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

help: ## Show this help.
	@awk 'BEGIN {FS = ":.*?## "; printf "convert2md — make targets\n\n"} /^[a-zA-Z_-]+:.*?## / {printf "  \033[1m%-9s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
