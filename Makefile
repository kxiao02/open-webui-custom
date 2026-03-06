
ifneq ($(shell which docker-compose 2>/dev/null),)
    DOCKER_COMPOSE := docker-compose
else
    DOCKER_COMPOSE := docker compose
endif

COMPOSE_BASE_FILES := -f docker-compose.yaml
COMPOSE_ADAPTER_FILES := -f docker-compose.yaml -f docker-compose.openai-adapter.yaml

.PHONY: install remove start startAndBuild stop update up-base up-adapter down down-base down-all logs-adapter ps-base ps-adapter

install:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) up -d

remove:
	@chmod +x confirm_remove.sh
	@./confirm_remove.sh

start:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) start

startAndBuild:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) up -d --build

stop:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) stop

up-base:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) up -d

up-adapter:
	$(DOCKER_COMPOSE) $(COMPOSE_ADAPTER_FILES) up -d --build

down-base:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) down

down-all:
	$(DOCKER_COMPOSE) $(COMPOSE_ADAPTER_FILES) down

down: down-all

logs-adapter:
	$(DOCKER_COMPOSE) $(COMPOSE_ADAPTER_FILES) logs -f openai-adapter

ps-base:
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) ps

ps-adapter:
	$(DOCKER_COMPOSE) $(COMPOSE_ADAPTER_FILES) ps

update:
	# Calls the LLM update script
	chmod +x update_ollama_models.sh
	@./update_ollama_models.sh
	@git pull
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) down
	# Make sure the ollama-webui container is stopped before rebuilding
	@docker stop open-webui || true
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) up --build -d
	$(DOCKER_COMPOSE) $(COMPOSE_BASE_FILES) start
