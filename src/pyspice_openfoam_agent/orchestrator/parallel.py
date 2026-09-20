"""Phase 15: bounded-concurrency evaluation of independent candidates.

WHY PROCESSES, NOT THREADS: spice/runner.py deliberately keeps ONE
process-wide shared ngspice instance (PySpice 1.5 crashes on a second
instance per process — audit finding 1.1). Two threads driving child
pipelines would interleave circuits on that single instance and corrupt
each other's runs. Every worker process here gets its own instance, its
own ToolContext and its own run dir — the "isolated artifacts, reproducible
parameters" contract the goals demand.

Best-effort contract (system-wide policy): a worker exception is captured
as {"error": ...} for that one candidate — a single bad candidate never
kills the batch. Pool startup/transport failures fall back to serial
in-process evaluation with the same per-task error capture.
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from typing import Callable


def _safe_call(pair: tuple[Callable[[dict], dict], dict]) -> dict:
    """Run one (worker, task) pair; never raise (Phase 15 error isolation)."""
    worker, task = pair
    try:
        return worker(task)
    except Exception as e:  # noqa: BLE001 — batch isolation by design
        return {"error": f"{type(e).__name__}: {e}"}


def evaluate_parallel(
    tasks: list[dict],
    worker: Callable[[dict], dict],
    max_workers: int = 2,
) -> list[dict]:
    """Evaluate `worker(task)` over `tasks` with a bounded process pool.

    Results come back in INPUT ORDER (deterministic reporting regardless of
    completion order). `max_workers <= 1` (or a single task) runs serially
    in-process — also the fallback if the pool cannot start. `worker` must
    be a module-level function (picklable under Windows/macOS spawn) and
    tasks must be plain dicts.
    """
    tasks = list(tasks)
    if max_workers <= 1 or len(tasks) <= 1:
        return [_safe_call((worker, t)) for t in tasks]
    try:
        with ProcessPoolExecutor(max_workers=min(max_workers, len(tasks))) as ex:
            return list(ex.map(_safe_call, [(worker, t) for t in tasks]))
    except Exception:
        # pool startup or transport failure — degrade to serial, same results
        return [_safe_call((worker, t)) for t in tasks]
