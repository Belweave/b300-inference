.PHONY: lint test dry-run
lint:
	./scripts/lint.sh
test:
	python3 -m unittest discover -s tests -v
dry-run:
	DRY_RUN=1 ./bootstrap.sh
	DRY_RUN=1 ./watcher/watch.sh --once
