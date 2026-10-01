.PHONY: up down install lint test migrate simulate ingest eval-retrieval eval-agent

SCENARIO ?= bad_deploy_payments

up:            ## Postgres + API in Docker
	docker compose up --build -d

down:
	docker compose down

install:
	cd backend && uv sync

lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy src

test:
	cd backend && TEST_DATABASE_URL=$${TEST_DATABASE_URL:-postgresql+psycopg://opsintel:opsintel@localhost:5432/opsintel_test} uv run pytest

migrate:
	cd backend && uv run opsintel migrate

simulate:      ## make simulate SCENARIO=payflow_outage
	cd backend && uv run opsintel simulate $(SCENARIO)

ingest:
	cd backend && uv run opsintel ingest-docs

eval-retrieval:  ## no LLM needed; gated in CI
	cd backend && uv run opsintel eval retrieval --out ../evals/results/retrieval.json && \
	uv run opsintel eval gate ../evals/results/retrieval.json

eval-agent:      ## needs LLM_PROVIDER=groq and GROQ_API_KEY; ~1 hour on the free tier
	cd backend && uv run opsintel eval agent --out ../evals/results/agent.json && \
	uv run opsintel eval gate ../evals/results/agent.json
