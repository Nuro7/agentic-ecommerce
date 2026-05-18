.PHONY: dev up down migrate logs test lint fmt shell

DEV_COMPOSE  = infra/docker/docker-compose.dev.yml
PROD_COMPOSE = infra/docker/docker-compose.yml

dev:
	docker compose -f $(DEV_COMPOSE) up

up:
	docker compose -f $(PROD_COMPOSE) up -d

down:
	docker compose -f $(DEV_COMPOSE) down

migrate:
	docker compose -f $(DEV_COMPOSE) exec app alembic upgrade head

logs:
	docker compose -f $(DEV_COMPOSE) logs -f app

test:
	cd backend && pytest tests/ -v

lint:
	cd backend && ruff check src/ tests/

fmt:
	cd backend && ruff format src/ tests/

shell:
	docker compose -f $(DEV_COMPOSE) exec app bash
