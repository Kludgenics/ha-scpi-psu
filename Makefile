PYTHON ?= python3

.PHONY: build-integration

build-integration:
	$(PYTHON) scripts/build_integration.py
