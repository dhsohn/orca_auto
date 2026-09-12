.PHONY: check check-packages test clean structural-tests

check:
	bash scripts/check.sh

test: check

check-packages:
	.venv/bin/python -m scripts.check_distributions

clean:
	bash scripts/clean_artifacts.sh

structural-tests:
	bash scripts/audit_structural_tests.sh
