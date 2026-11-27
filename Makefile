# ============================================================================
# example-org/mcp-plugins - local entry point
# ============================================================================
#
# CI is the authoritative gate (.gitlab-ci.yml); these targets run the same
# checks locally.
#
# ============================================================================

.PHONY: help setup bump release lint lint-yaml lint-markdown lint-shell lint-manifests lint-plugin lint-python typecheck test audit smoke-live eval validate clean
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

# The pre-release gates need live credentials and model usage, so they run
# here rather than in CI. SKIP_EVAL=1 for a release that changes no skill or
# tool description.
release: ## smoke-live and eval, then bump (PART, default from the commits) and add the CHANGELOG.md section
	@part=$(or $(PART),$$(python3 scripts/changelog.py --part)) && \
		echo "release: $$part bump" && \
		$(MAKE) --no-print-directory smoke-live && \
		$(if $(SKIP_EVAL),echo "release: eval skipped (SKIP_EVAL)",$(MAKE) --no-print-directory eval) && \
		python3 scripts/bump_version.py $$part && \
		python3 scripts/changelog.py

lint: lint-yaml lint-markdown lint-shell lint-manifests lint-plugin lint-python typecheck ## Run every linter

lint-yaml: ## yamllint (lint:yaml)
	@yamllint -c .config/.yamllint .

lint-markdown: ## markdownlint-cli2 (lint:markdown)
	@markdownlint-cli2 --config .config/.markdownlint-cli2.jsonc "**/*.md"

lint-shell: ## shellcheck (lint:shell)
	@git ls-files -z '*.sh' | xargs -0 -r shellcheck --rcfile=.config/.shellcheckrc

lint-manifests: ## Versions, pins, skill tool names and skill frontmatter (lint:manifests)
	@python3 .ci/check_manifests.py
	@python3 .ci/check_skill_tools.py
	@python3 .ci/check_skills.py

lint-plugin: ## Claude Code manifest validation (lint:plugin)
	@claude plugin validate --strict .

lint-python: ## ruff check and format check (test:python)
	@cd $(SERVER) && uv run --locked ruff check src tests && uv run --locked ruff format --check src tests

typecheck: ## pyright in strict mode (test:python)
	@cd $(SERVER) && uv run --locked pyright

test: ## pytest with branch coverage and its floor (test:python)
	@cd $(SERVER) && uv run --locked pytest --cov

# The same pinned trivy as the CI image; needs Docker.
TRIVY_IMAGE := aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb
# Real model calls on your account: each case runs 3 times with the plugin and 3
# times without it. Mocked MCP servers; no backend is touched.
EVAL_ARGS ?=
eval: ## Behaviour evals of the plugin's skills and tools (before a release; costs model usage)
	@cd plugins/homelab && claude plugin eval . --trust-plugin --no-publish -j 4 \
		--threshold 0.8 --max-cost-usd 15 $(EVAL_ARGS)

smoke-live: ## Start the third-party servers with live credentials; check tool names (before a release)
	@cd $(SERVER) && uv run --locked python ../../../.ci/smoke_third_party.py

audit: ## Known vulnerabilities in uv.lock and the npm servers' lockfile (audit:deps)
	@docker run --rm -v "$(CURDIR)/plugins/homelab:/src:ro" $(TRIVY_IMAGE) fs --quiet --scanners vuln \
		--skip-dirs /src/server/.venv --exit-code 1 --ignore-unfixed /src

validate: lint test ## Everything CI checks

clean: ## Remove caches and test output
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@rm -rf $(SERVER)/.pytest_cache $(SERVER)/.ruff_cache
