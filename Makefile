.PHONY: test lint typecheck validate docker-conformance

test:
	.venv/bin/pytest --cov --cov-report=term-missing -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

validate: lint typecheck test

docker-conformance:
	@set -eu; \
	cleanup() { \
		docker compose -p pullbox-provider-conformance \
			-f docker/compose.synthetic-test.yml \
			down --volumes --remove-orphans; \
	}; \
	trap cleanup EXIT INT TERM; \
	docker compose -p pullbox-provider-conformance \
		-f docker/compose.synthetic-test.yml \
		up --build --abort-on-container-exit --exit-code-from conformance
