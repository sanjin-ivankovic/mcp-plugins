# ============================================================================
# example-org/mcp-plugins - local entry point
# ============================================================================
#
# CI is the authoritative gate (.gitlab-ci.yml); these targets run the same
# checks locally.
#
# ============================================================================

.PHONY: help setup bump lint lint-yaml lint-markdown lint-shell lint-manifests lint-python test validate clean
.DEFAULT_GOAL := help

SERVER := plugins/homelab/server

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install pre-commit hooks and the server's dev environment
	@pre-commit install
	@pre-commit install --hook-type commit-msg
	@cd $(SERVER) && uv sync --frozen

bump: ## Bump the plugin version in all four places (PART=patch|minor|major)
	@python3 scripts/bump_version.py $(or $(PART),patch)

lint: lint-yaml lint-markdown lint-shell lint-manifests lint-python ## Run every linter

lint-yaml: ## yamllint (lint:yaml)
	@yamllint -c .config/.yamllint .

lint-markdown: ## markdownlint-cli2 (lint:markdown)
	@markdownlint-cli2 --config .config/.markdownlint-cli2.jsonc "**/*.md"

lint-shell: ## shellcheck (lint:shell)
	@git ls-files -z '*.sh' | xargs -0 -r shellcheck --rcfile=.config/.shellcheckrc

lint-manifests: ## Plugin manifest and version check (lint:manifests)
	@python3 .ci/check_manifests.py

lint-python: ## ruff check and format check (test:python)
	@cd $(SERVER) && uv run ruff check src tests && uv run ruff format --check src tests

test: ## pytest for the homelab server (test:python)
	@cd $(SERVER) && uv run pytest

validate: lint test ## Everything CI checks

clean: ## Remove caches and test output
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache
