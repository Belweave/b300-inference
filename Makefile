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
	@# node_run end to end off the node: fake engine and guards, mocked verdicts, a late
	@# tunnel failure that keeps the engine, then RESUME through guards and relay-safety.
	$(PYTHON) -m unittest discover -v -s tests -p test_node_run.py -k test_rehearsal_guards_relay_safety_and_resume
