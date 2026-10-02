"""A small ordered process pool for the bounded pipeline's independent jobs.

`multiprocessing` start methods that are safe next to DuckDB and SQLAlchemy
threads (spawn, forkserver) re-import the caller's `__main__` in every worker,
which re-runs any script that lacks a main guard. These workers are plain
`python -m app.engine.process_pool` processes instead: they import only the
job's own module, from the same code tree as the parent, and talk over their
stdin/stdout with length-prefixed pickles.

    with ProcessPool(4) as pool:
        for result in pool.imap("app.engine.bounded:_partition_job", jobs):
            ...

Results come back in job order. A job that raises, or a worker that dies (for
example killed for memory), fails the whole map with the worker's traceback.
"""

from __future__ import annotations

import importlib
import os
import pickle
import struct
import subprocess
import sys
import threading
import traceback
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, BinaryIO, Self

_HEADER = struct.Struct("!Q")


class WorkerError(RuntimeError):
    """A job failed in a worker process."""


def _send(stream: BinaryIO, payload: Any) -> None:
    data = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(data)))
    stream.write(data)
    stream.flush()


def _receive(stream: BinaryIO) -> Any:
    header = stream.read(_HEADER.size)
    if len(header) < _HEADER.size:
        raise EOFError
    (size,) = _HEADER.unpack(header)
    data = stream.read(size)
    if len(data) < size:
        raise EOFError
    return pickle.loads(data)


def _resolve(target: str):
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


class ProcessPool:
    def __init__(self, workers: int) -> None:
        if workers < 1:
            raise ValueError("a pool needs at least one worker")
        # The `app` package this module belongs to: workers import the same tree.
        root = str(Path(__file__).resolve().parents[2])
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._processes = [
            subprocess.Popen(
                [sys.executable, "-m", "app.engine.process_pool"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env, cwd=os.getcwd(),
            )
            for _ in range(workers)
        ]

    def imap(self, target: str, jobs: Iterable[Any]) -> Iterator[Any]:
        """target(job) for every job ("module:function"), yielded in job order."""
        jobs = list(jobs)
        results: dict[int, tuple[bool, Any]] = {}
        condition = threading.Condition()
        next_job = iter(range(len(jobs)))
        lock = threading.Lock()

        def drive(process: subprocess.Popen) -> None:
            while True:
                with lock:
                    index = next(next_job, None)
                if index is None:
                    return
                try:
                    _send(process.stdin, (target, jobs[index]))
                    outcome = _receive(process.stdout)
                except (EOFError, BrokenPipeError, OSError):
                    outcome = (False, f"worker process exited (code {process.poll()}) during job {index}")
                with condition:
                    results[index] = outcome
                    condition.notify_all()
                if not outcome[0]:
                    return

        threads = [threading.Thread(target=drive, args=(process,), daemon=True) for process in self._processes]
        for thread in threads:
            thread.start()
        for index in range(len(jobs)):
            with condition:
                while index not in results:
                    if not any(thread.is_alive() for thread in threads) and index not in results:
                        raise WorkerError(f"no worker left to run job {index}")
                    condition.wait(timeout=1.0)
                ok, value = results.pop(index)
            if not ok:
                raise WorkerError(value)
            yield value

    def close(self) -> None:
        for process in self._processes:
            try:
                if process.stdin:
                    process.stdin.close()
            except OSError:
                pass
        for process in self._processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        if exc[0] is not None:
            for process in self._processes:
                if process.poll() is None:
                    process.kill()
        self.close()


def _serve() -> None:
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
    # Anything a job prints must not corrupt the result stream.
    sys.stdout = sys.stderr
    while True:
        try:
            target, job = _receive(reader)
        except EOFError:
            return
        try:
            outcome = (True, _resolve(target)(job))
        except BaseException:  # noqa: BLE001 - reported to the parent, which raises
            outcome = (False, traceback.format_exc())
        _send(writer, outcome)


if __name__ == "__main__":
    _serve()
