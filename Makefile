.PHONY: help setup install test lint fmt scan run discover doctor docker clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS=":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create venv, install, seed .env, init database
	./setup.sh

install: ## Install with dev + embedding extras
	pip install -e ".[dev,embeddings]"

test: ## Run the test suite
	pytest -q

lint: ## Lint
	ruff check src tests

fmt: ## Auto-fix lint issues
	ruff check --fix src tests

scan: ## One scan cycle, no notifications
	jobmon scan --no-notify

run: ## Start the daemon
	jobmon run

discover: ## Detect careers platforms and write companies.yaml
	jobmon discover

doctor: ## Check config, endpoints and channels
	jobmon doctor

docker: ## Build the image
	docker build -t jobmonitor:latest .

clean: ## Remove caches and build artefacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
