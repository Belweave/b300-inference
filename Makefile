PYTHON ?= $(if $(wildcard .venv/bin/python3),.venv/bin/python3,python3)
.PHONY: lint test dry-run
lint:
	./scripts/lint.sh
test:
	$(PYTHON) -m unittest discover -s tests -v
dry-run:
	DRY_RUN=1 NETWORK_MODE=ssh WEIGHTS_SOURCE=push ./bootstrap.sh
	DRY_RUN=1 NETWORK_MODE=auto ./bootstrap.sh
	DRY_RUN=1 ./scripts/prestage.sh
	DRY_RUN=1 ./watcher/watch.sh --once
