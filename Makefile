.PHONY: check test clean structural-tests

check:
	bash scripts/check.sh

test: check

clean:
	bash scripts/clean_artifacts.sh

structural-tests:
	bash scripts/audit_structural_tests.sh
