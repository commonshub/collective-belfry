# Belfry local stack. See infra/README.md.
COMPOSE := docker compose -f infra/docker-compose.yml

.PHONY: up up-app build-keycast bootstrap smoke down logs test-odoo test-onboarding secrets

infra/.env:
	infra/scripts/gen-secrets.sh

secrets: ## (re)generate infra/.env, infra/secrets/ and the Keycast master key (keeps existing files)
	infra/scripts/gen-secrets.sh

up: infra/.env ## start postgres, odoo, authentik, keycast, relay (not the onboarding service)
	mkdir -p odoo/addons
	$(COMPOSE) up -d

up-app: infra/.env ## start everything including the onboarding service (needs services/onboarding)
	mkdir -p odoo/addons
	$(COMPOSE) --profile app build onboarding
	$(COMPOSE) --profile app up -d

build-keycast: ## build belfry-keycast:local from infra/keycast/build/Dockerfile (about 15 minutes cold)
	$(COMPOSE) build keycast

bootstrap: ## create the Odoo database, users, tokens, Keycast admin and policy; writes tokens into infra/secrets/
	infra/scripts/bootstrap.sh

smoke: ## invite -> pay -> accounts created once (needs up-app and bootstrap)
	infra/scripts/smoke.sh

down: ## stop the stack, keep volumes
	$(COMPOSE) --profile app down

logs:
	$(COMPOSE) --profile app logs -f --tail=100

test-odoo: ## run the belfry_pass tests in a throwaway Odoo container against the stack's postgres
	$(COMPOSE) exec -T postgres psql -U postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'test_belfry'" -c 'DROP DATABASE IF EXISTS test_belfry'
	$(COMPOSE) run --rm --no-deps -p 8169:8069 odoo odoo -d test_belfry --db-filter='^test_belfry$$' -i belfry_pass --test-enable --test-tags /belfry_pass --stop-after-init --without-demo

test-onboarding:
	cd services/onboarding && pytest
