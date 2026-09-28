.PHONY: phase-zero phase-one phase-two phase-two-100k phase-three phase-four phase-five-a-smoke phase-five-b-compare verify-fixture test

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
