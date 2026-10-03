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


def test_duckdb_children_have_independent_existing_spill_directories(tmp_path):
    from app.engine.bounded import FactStore

    parent = FactStore(tmp_path / "facts")
    parent.con.execute("CREATE TABLE test_values AS SELECT 1 AS value")
    parent.suspend()
    children = []
    try:
        children = [FactStore.attach(parent.path, memory_limit_mb=128, plan=parent.plan,
                                     tables=("test_values",)) for _ in range(2)]
        assert children[0]._spill != children[1]._spill
        assert all(child._spill.is_dir() for child in children)
        for child in children:
            assert child.con.execute("SELECT value FROM test_values").fetchone()[0] == 1
        first_spill, second_spill = [child._spill for child in children]
        children[0].close()
        assert not first_spill.exists() and second_spill.exists()
    finally:
        for child in children[1:]:
            child.close()
        parent.resume()
        parent.close()
