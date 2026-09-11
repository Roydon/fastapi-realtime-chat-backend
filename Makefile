COMPOSE ?= docker compose
PYTEST_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help up down logs ps rebuild smoke test test-cov lint fmt typecheck migrate openapi loadtest clean

help: ## List targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

.env:
	cp .env.example .env

up: .env ## Build and start the full stack, wait for health
	$(COMPOSE) up -d --build --wait --wait-timeout 180
	@echo "Demo:       http://localhost:8080/demo/"
	@echo "OpenAPI:    http://localhost:8080/docs"
	@echo "Prometheus: http://localhost:9090"

down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v --remove-orphans

logs: ## Tail API logs
	$(COMPOSE) logs -f api1 api2

ps: ## Show container status
	$(COMPOSE) ps

rebuild: ## Rebuild images without cache
	$(COMPOSE) build --no-cache

smoke: ## Cross-replica Alice<->Bob check against the running stack
	uv run python scripts/smoke.py

test: ## Run the test suite (spins up throwaway Postgres/Redis/MinIO)
	uv run pytest $(PYTEST_ARGS)

test-cov: ## Run tests with coverage gate (>= 85% on app/)
	uv run pytest --cov=app --cov-report=term-missing --cov-report=xml $(PYTEST_ARGS)

lint: ## ruff + mypy
	uv run ruff check app tools tests
	uv run ruff format --check app tools tests
	uv run mypy app tools

fmt: ## Auto-format and fix
	uv run ruff format app tools tests
	uv run ruff check --fix app tools tests

migrate: ## Apply migrations against the compose Postgres
	$(COMPOSE) run --rm migrate

openapi: ## Export docs/openapi.json from the app
	uv run python scripts/export_openapi.py

loadtest: ## k6 WebSocket load test (needs k6; override VUS=/DURATION=)
	k6 run tools/loadtest.js

clean: down ## Full clean: containers, volumes, caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
