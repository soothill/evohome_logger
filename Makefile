PODMAN ?= podman
IMAGE ?= evohome-logger
TAG ?= latest
CONFIG_FILE ?= config.env
# Rootless-friendly default; override via HOST_DATA_DIR in the config file or on the command line
DEFAULT_HOST_DATA_DIR := $(HOME)/.local/share/evohome-logger
CONFIG_HOST_DATA_DIR := $(shell if [ -f $(CONFIG_FILE) ]; then sed -n 's/^HOST_DATA_DIR=//p' $(CONFIG_FILE) | tail -n1; fi)
ifeq ($(strip $(CONFIG_HOST_DATA_DIR)),)
HOST_DATA_DIR ?= $(DEFAULT_HOST_DATA_DIR)
else
HOST_DATA_DIR ?= $(CONFIG_HOST_DATA_DIR)
endif

CONTAINER_NAME ?= evohome-logger

.DEFAULT_GOAL := help

.PHONY: help build config run-once run-detached logs stop rm test-connect

help:
	@echo "Evohome logger (Podman) targets:"
	@echo "  make build         - Build the container image ($(IMAGE):$(TAG))"
	@echo "  make config        - Copy config.env.example to config.env if missing"
	@echo "  make run-once      - Run container once with config/env + data volume"
	@echo "  make run-detached  - Run container detached with name $(CONTAINER_NAME)"
	@echo "  make logs          - Follow logs for $(CONTAINER_NAME)"
	@echo "  make stop          - Stop detached container (if running)"
	@echo "  make rm            - Remove stopped container"
	@echo "  make test-connect  - Connectivity check only (Evohome + InfluxDB, no writes)"

build:
	$(PODMAN) build -t $(IMAGE):$(TAG) .

config:
	@if [ ! -f $(CONFIG_FILE) ]; then cp config.env.example $(CONFIG_FILE) && echo "Created $(CONFIG_FILE); edit it with your credentials."; else echo "Config file $(CONFIG_FILE) already exists."; fi

run-once: build
	mkdir -p $(HOST_DATA_DIR)
	$(PODMAN) run --rm --env-file $(CONFIG_FILE) -v $(HOST_DATA_DIR):/data $(IMAGE):$(TAG)

run-detached: build
	mkdir -p $(HOST_DATA_DIR)
	$(PODMAN) run --replace -d --name $(CONTAINER_NAME) --env-file $(CONFIG_FILE) -v $(HOST_DATA_DIR):/data $(IMAGE):$(TAG)

test-connect: build
	mkdir -p $(HOST_DATA_DIR)
	$(PODMAN) run --rm --env-file $(CONFIG_FILE) -v $(HOST_DATA_DIR):/data $(IMAGE):$(TAG) --check

logs:
	$(PODMAN) logs -f $(CONTAINER_NAME)

stop:
	-$(PODMAN) stop $(CONTAINER_NAME)

rm:
	-$(PODMAN) rm $(CONTAINER_NAME)
