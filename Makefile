# =============================================================================
# Enterprise AI Platform — operator shortcuts
# =============================================================================
SHELL := /bin/bash
ENV_FILE ?= .env
DC := docker compose --env-file $(ENV_FILE)

.DEFAULT_GOAL := help

.PHONY: help install upgrade uninstall up down restart ps logs health \
        backup restore build pull reload-nginx issue-certs change-domain \
        monitoring-up monitoring-down scale-workers config validate test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Run the full installer (sudo)
	sudo ./install.sh

upgrade: ## Pull/rebuild images and recreate services (sudo)
	sudo ./upgrade.sh

uninstall: ## Stop & remove containers (keep data)
	sudo ./uninstall.sh

up: ## Start the whole stack
	$(DC) up -d

down: ## Stop the stack (keep data)
	$(DC) down

restart: ## Restart all services
	$(DC) restart

ps: ## Show service status
	$(DC) ps

logs: ## Tail logs (use S=<service> to filter, e.g. make logs S=nginx)
	$(DC) logs -f --tail=200 $(S)

health: ## Run the health check
	./healthcheck.sh

backup: ## Run a full backup
	sudo ./scripts/backup.sh

restore: ## Restore from a backup dir: make restore SRC=backups/<ts>
	sudo ./scripts/restore.sh $(SRC)

build: ## Build local images (Keycloak, LangFlow)
	$(DC) build

pull: ## Pull all images
	$(DC) pull

reload-nginx: ## Reload NGINX configuration
	$(DC) exec nginx nginx -s reload

issue-certs: ## (Re)issue Let's Encrypt certificates
	sudo ./scripts/issue-certs.sh

change-domain: ## Change the base domain post-install: make change-domain D=new.example.com
	sudo ./scripts/change-domain.sh --domain $(D)

scale-workers: ## Scale LangFlow workers: make scale-workers N=4
	$(DC) up -d --scale langflow-worker=$(N)

monitoring-up: ## Start the monitoring stack (Prometheus/Grafana/exporters)
	$(DC) -f docker-compose.yml -f monitoring/docker-compose.monitoring.yml --profile monitoring up -d

monitoring-down: ## Stop the monitoring stack
	$(DC) -f docker-compose.yml -f monitoring/docker-compose.monitoring.yml --profile monitoring down

config: ## Render and validate the merged compose config
	$(DC) config

validate: ## Validate compose, shell scripts and YAML locally
	$(DC) config -q && echo "compose: OK"
	@command -v shellcheck >/dev/null && shellcheck -x --source-path=SCRIPTDIR install.sh upgrade.sh uninstall.sh healthcheck.sh scripts/*.sh || echo "shellcheck not installed (skipped)"

test: ## Run offline self-tests (no Docker required)
	@rc=0; for t in scripts/*.selftest.sh; do \
		echo "== $$t =="; bash "$$t" || rc=1; \
	done; exit $$rc
