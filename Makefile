.PHONY: test lint typecheck validate security-check docker-conformance docker-source-smoke

test:
	.venv/bin/pytest --cov --cov-report=term-missing -q

lint:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

typecheck:
	.venv/bin/mypy

validate: lint typecheck test

security-check:
	.venv/bin/bandit -r packages providers -ll -ii
	.venv/bin/pip-audit --strict .

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

docker-source-smoke:
	@set -eu; \
	cleanup() { \
		docker compose -p pullbox-provider-source-smoke \
			-f docker/compose.providers-test.yml \
			down --volumes --remove-orphans; \
	}; \
	trap cleanup EXIT INT TERM; \
	docker compose -p pullbox-provider-source-smoke \
		-f docker/compose.providers-test.yml \
		up --build --abort-on-container-exit --exit-code-from smoke
