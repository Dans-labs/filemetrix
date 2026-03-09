# Makefile for common developer tasks in FileMetrix
VENV=.venv
PYTHON=${VENV}/bin/python
UVCMD=uvicorn src.filemetrix.main:app
ORG_NAME := dans-labs
IMAGE_NAME := $(ORG_NAME)/filemetrix-service
VERSION = $(shell grep '^version' pyproject.toml | head -1 | cut -d '"' -f2)

.PHONY: help venv install build run run-dev compose-up compose-down lint test

help:
	@echo "Available targets: venv install build run run-dev compose-up compose-down lint test"

venv:
	python -m venv ${VENV}
	${VENV}/bin/pip install --upgrade pip

install: venv
	${VENV}/bin/pip install -e .

build:
	docker build -t filemetrix-service .

run:
	# Run the service in foreground (production-ish)
	${PYTHON} -m src.filemetrix.main

run-dev:
	# Run with autoreload for development
	${VENV}/bin/uvicorn src.filemetrix.main:app --reload --host 0.0.0.0 --port 1966

compose-up:
	docker-compose up -d --build

compose-down:
	docker-compose down

lint:
	# placeholder for linters, e.g. flake8 or ruff
	@if [ -x "${VENV}/bin/ruff" ]; then ${VENV}/bin/ruff src; else echo "Install ruff in venv to run lint"; fi

test:
	# Run tests if present
	@if [ -x "${VENV}/bin/pytest" ]; then ${VENV}/bin/pytest -q; else echo "No tests or pytest not installed"; fi

print-version:
	@echo "Version: $(VERSION)"

docker-build:
	@echo "Building $(IMAGE_NAME):$(VERSION)"
	uv lock
	docker build --platform linux/amd64 -t ghcr.io/$(IMAGE_NAME):$(VERSION) .
	docker tag ghcr.io/$(IMAGE_NAME):$(VERSION) ghcr.io/$(IMAGE_NAME):latest

