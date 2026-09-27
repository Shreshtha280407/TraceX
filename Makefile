.PHONY: phase-zero phase-one phase-two phase-two-100k phase-three phase-four verify-fixture test

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
