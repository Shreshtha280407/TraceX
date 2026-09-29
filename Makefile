.PHONY: phase-zero phase-one phase-two phase-two-100k phase-three phase-four phase-five-a-smoke phase-five-b-compare verify-fixture test dataset anomaly-stack anomaly-stack-demo anomaly-stack-holdout anomaly-stack-test

phase-zero: verify-fixture test

verify-fixture:
	python3 fixtures/demo_100k/generate.py --verify

test:
	python3 -m unittest discover -s tests -v

phase-one:
	uv run ruff check app workers tests/unit/test_phase_one.py
	uv run pytest -q tests/unit/test_phase_one.py

phase-two:
	uv run ruff check app workers tests/unit/test_phase_one.py tests/unit/test_phase_two.py scripts/phase2_100k_smoke.py
	uv run pytest -q tests/unit/test_phase_one.py tests/unit/test_phase_two.py

phase-two-100k:
	uv run python scripts/phase2_100k_smoke.py

phase-three:
	uv run ruff check app workers tests/unit/test_phase_one.py tests/unit/test_phase_two.py tests/unit/test_phase_three.py scripts/phase2_100k_smoke.py
	uv run pytest -q tests/unit/test_phase_one.py tests/unit/test_phase_two.py tests/unit/test_phase_three.py

phase-four:
	uv run ruff check app workers tests/unit/test_phase_one.py tests/unit/test_phase_two.py tests/unit/test_phase_three.py tests/unit/test_phase_four.py scripts/phase2_100k_smoke.py
	uv run pytest -q tests/unit/test_phase_one.py tests/unit/test_phase_two.py tests/unit/test_phase_three.py tests/unit/test_phase_four.py

phase-five-a-smoke:
	UV_CACHE_DIR=/tmp/tracex-uv-cache uv run python scripts/phase5a_smoke.py

phase-five-b-compare:
	@if [ ! -d datasets/phase5a_100k ]; then \
		echo "Phase 5A 100K fixture not found at datasets/phase5a_100k/. Generate it first:"; \
		echo "  python3 fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson --verify"; \
		exit 1; \
	fi
	uv run --extra ml python scripts/phase5b_compare.py

# ---------------------------------------------------------------------------
# Anomaly stack (generator v2 fixture + the six-layer stack)
# ---------------------------------------------------------------------------

# Regenerate the 100K fixture in all four ingestion formats and verify every
# invariant and manifest hash.  ~35 s, ~350 MB of output.
#
# Uses `uv run python`, the project-managed interpreter pinned by pyproject.toml
# (requires-python >=3.11), rather than the bare `python3` on PATH. A bare
# `python3` is whatever the OS shipped -- macOS's system Python is 3.9-3.10 on
# many installs, which is below the floor this generator needs and fails with
# an opaque syntax/typing error instead of a clear version message. `uv sync`
# has already resolved and pinned the right interpreter before this ever runs.
dataset:
	uv run python fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson,xml,json --verify

# Run every layer alone, every combination, and the deterministic rule baseline
# on the validation split, for all three tasks.  Unsupervised only -- this is the
# configuration intended for deployment, and the one whose numbers are quotable.
anomaly-stack: | datasets/phase5a_100k
	uv run --extra ml python scripts/run_anomaly_stack.py \
		--dataset datasets/phase5a_100k \
		--output experiments/runs/anomaly_stack_validation.json

# Adds the supervised comparator. Demo/research only: on this fixture layer S
# trains on generator truth, which is close to circular for the motif task.
anomaly-stack-demo: | datasets/phase5a_100k
	uv run --extra ml python scripts/run_anomaly_stack.py \
		--dataset datasets/phase5a_100k \
		--with-supervised \
		--output experiments/runs/anomaly_stack_demo.json

# The final holdout is evaluated exactly once, after every setting is frozen.
# Running this before `anomaly-stack` defeats the point of having a holdout.
anomaly-stack-holdout: | datasets/phase5a_100k
	uv run --extra ml python scripts/run_anomaly_stack.py \
		--dataset datasets/phase5a_100k \
		--split final_holdout \
		--output experiments/runs/anomaly_stack_holdout.json

anomaly-stack-test:
	uv run --extra ml ruff check app/ml scripts/run_anomaly_stack.py fixtures/phase5a_100k/generate.py tests/unit/test_ml_pipeline_integration.py
	uv run --extra ml pytest -q tests/unit/test_anomaly_stack.py tests/unit/test_ml_pipeline_integration.py \
		tests/unit/test_phase5a_fixture_v2.py tests/unit/test_phase5a_100k_dataset.py

datasets/phase5a_100k:
	@echo "No fixture at datasets/phase5a_100k. Run: make dataset"
	@echo "(that runs: uv run python fixtures/phase5a_100k/generate.py --output datasets/phase5a_100k --formats csv,ndjson,xml,json --verify)"
	@exit 1
