"""The bounded pipeline's worker pool: ordered results, failures surface."""

from __future__ import annotations

import pytest

from app.engine.process_pool import ProcessPool, WorkerError


def test_results_come_back_in_job_order() -> None:
    jobs = [float(n * n) for n in range(40)]
    with ProcessPool(3) as pool:
        assert list(pool.imap("math:sqrt", jobs)) == [float(n) for n in range(40)]


def test_a_failing_job_fails_the_map_with_its_traceback() -> None:
    with pytest.raises(WorkerError, match="math domain error"), ProcessPool(2) as pool:
        list(pool.imap("math:sqrt", [4.0, -1.0, 9.0]))


def test_a_worker_that_dies_fails_the_map() -> None:
    with pytest.raises(WorkerError), ProcessPool(1) as pool:
        list(pool.imap("os:_exit", [3]))


def test_jobs_are_drawn_lazily_and_errors_while_drawing_arrive_in_order() -> None:
    drawn = []

    def jobs():
        for n in range(6):
            drawn.append(n)
            yield float(n)
        raise KeyError("source broke")

    with ProcessPool(2) as pool:
        results = pool.imap("math:sqrt", jobs(), window=2)
        assert next(results) == 0.0
        assert len(drawn) <= 4
        seen = [0.0]
        with pytest.raises(KeyError, match="source broke"):
            seen.extend(results)
    assert seen == [float(n) ** 0.5 for n in range(6)]
