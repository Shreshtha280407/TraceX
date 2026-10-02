.PHONY: phase-zero phase-one phase-two phase-two-100k phase-three phase-four phase-five-a-smoke phase-five-b-compare verify-fixture test dataset anomaly-stack anomaly-stack-demo anomaly-stack-holdout anomaly-stack-test phase-six phase-six-throughput phase-six-throughput-1m phase-six-query-latency phase-six-test phase-seven phase-seven-test phase-seven-walkthrough

# Local-dev-only convenience: lets every recipe below authenticate scripts/tests
# via the X-TraceX-Actor header instead of a real signup/login round trip. A
# real deployment must never set this -- app.config.Settings.from_environment()
# defaults it to disabled precisely so this Makefile is the only place it's on.
export TRACEX_ALLOW_DEV_ACTOR_HEADER := 1

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

# ---------------------------------------------------------------------------
# Phase 6 (performance, recovery, security)
# ---------------------------------------------------------------------------

phase-six: phase-six-test phase-six-throughput phase-six-query-latency

# Real pipeline (HTTP upload -> worker parse/commit -> graph -> deterministic
# findings/features), staged timings, against the generator-v2 100K fixture.
phase-six-throughput: | datasets/phase5a_100k
	uv run python scripts/phase6_throughput.py --scale 100k --output experiments/runs/phase6_throughput_100k.json

# Same pipeline at 1,000,000 rows via a lightweight synthetic linear-chain
# generator (the labelled fixture generator has no size parameter). Slow and
# memory-heavy -- see docs/phase6.md for what this host could and could not
# complete.
phase-six-throughput-1m:
	uv run python scripts/phase6_throughput.py --scale 1m --output experiments/runs/phase6_throughput_1m.json

# 200+ bounded graph queries at 1 and 4 concurrent readers, including the
# fixture's actual highest-degree node.
phase-six-query-latency: | datasets/phase5a_100k
	uv run python scripts/phase6_query_latency.py --output experiments/runs/phase6_query_latency.json

phase-six-test:
	uv run ruff check app/engine/findings/deterministic.py scripts/phase6_throughput.py scripts/phase6_query_latency.py tests/unit/test_phase_six_crash_retry.py tests/unit/test_phase_six_offline_and_access.py tests/unit/test_phase_four.py
	uv run pytest -q tests/unit/test_phase_six_crash_retry.py tests/unit/test_phase_six_offline_and_access.py tests/unit/test_phase_four.py tests/unit/test_auth_and_case_reads.py

# ---------------------------------------------------------------------------
# Phase 7 (submission proof package)
# ---------------------------------------------------------------------------

phase-seven: phase-seven-test phase-seven-walkthrough

# Offline-boot proof (socket guard installed before any `app.*` import) and
# pinned-dependency-manifest checks, plus the evidence-export honesty fix
# (export/evidence must reflect which rule/model actually produced a row,
# never a hardcoded label) and its regression coverage.
phase-seven-test:
	uv run ruff check app/api/routes.py scripts/phase7_case_walkthrough.py tests/unit/test_phase_seven_offline_launch.py tests/unit/test_ml_pipeline_integration.py
	uv run --extra ml pytest -q tests/unit/test_phase_seven_offline_launch.py tests/unit/test_ml_pipeline_integration.py tests/unit/test_phase_four.py

# The full case walkthrough over the real API and worker: original file ->
# progress events -> committed graph -> deterministic lead -> model rank ->
# source rows -> analyst decision -> case-scoped export. Also the
# accuracy-honesty proof: one coinjoin_like scenario and one
# nearmiss_rule_positive high-volume benign lookalike, both opened down to
# their source record. See docs/phase7.md.
phase-seven-walkthrough: | datasets/phase5a_100k
	uv run --extra ml python scripts/phase7_case_walkthrough.py --output experiments/runs/phase7_case_walkthrough.json

# ---------------------------------------------------------------------------
# Offline Geo-IP database, dataset intake, offline Linux appliance
# ---------------------------------------------------------------------------
.PHONY: geoip appliance offline-bundle calibrate-confidence

# Fetch DB-IP country lite (CC BY 4.0) + IPtoASN (PDDL) once and compile them into
# var/geoip (TRACEX_GEOIP_DIR). Offline hosts: `tracex-geoip import FILE...` instead.
geoip:
	uv run python -m app.engine.geoip download

# Build the appliance image (API + worker + web UI + ML + Geo-IP).
appliance:
	docker build -t tracex-appliance:latest .

# Image + compose stack + installer in one tarball for air-gapped Linux hosts.
offline-bundle:
	scripts/build_offline_bundle.sh

# Refit app/engine/calibration/confidence-v1.json from a database produced by
# importing datasets/phase5a_100k/ingestion_rows.ndjson (labels are read only here).
calibrate-confidence:
	uv run --extra ml python scripts/calibrate_confidence.py --database $(DB) --dataset datasets/phase5a_100k
