.PHONY: check check-packages test clean

check:
	bash scripts/check.sh

test: check

check-packages:
	.venv/bin/python -m scripts.check_distributions

clean:
	bash scripts/clean_artifacts.sh
