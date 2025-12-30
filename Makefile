PODMAN ?= podman
IMAGE ?= evohome-logger
TAG ?= latest
CONFIG_FILE ?= config.env
DATA_DIR ?= /var/lib/evohome-logger
CONTAINER_NAME ?= evohome-logger

.PHONY: build config run-once run-detached logs stop rm test-connect

build:
	$(PODMAN) build -t $(IMAGE):$(TAG) .

config:
	@if [ ! -f $(CONFIG_FILE) ]; then cp config.env.example $(CONFIG_FILE) && echo "Created $(CONFIG_FILE); edit it with your credentials."; else echo "Config file $(CONFIG_FILE) already exists."; fi

run-once: build
	mkdir -p $(DATA_DIR)
	$(PODMAN) run --rm --env-file $(CONFIG_FILE) -v $(DATA_DIR):/data $(IMAGE):$(TAG)

run-detached: build
	mkdir -p $(DATA_DIR)
	$(PODMAN) run --replace -d --name $(CONTAINER_NAME) --env-file $(CONFIG_FILE) -v $(DATA_DIR):/data $(IMAGE):$(TAG)

test-connect: build
	mkdir -p $(DATA_DIR)
	$(PODMAN) run --rm --env-file $(CONFIG_FILE) -v $(DATA_DIR):/data $(IMAGE):$(TAG) --check

logs:
	$(PODMAN) logs -f $(CONTAINER_NAME)

stop:
	-$(PODMAN) stop $(CONTAINER_NAME)

rm:
	-$(PODMAN) rm $(CONTAINER_NAME)
