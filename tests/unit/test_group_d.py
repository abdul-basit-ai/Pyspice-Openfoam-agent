"""Group D (remain_plan): Phase 15 bounded process-parallel evaluation.

Phase 15 contract under test: independent candidates evaluate concurrently
with ISOLATED artifacts, one bad candidate never kills the batch, results
come back in input order, and everything degrades to serial safely.
Phase 21 / descoped items carry goals-doc status notes only (no code).
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.orchestrator.parallel import evaluate_parallel
from pyspice_openfoam_agent.orchestrator.tools import (
    TOOL_SCHEMAS,
    ToolContext,
    dispatch,
)


def _demo_worker(task: dict) -> dict:
    """Module-level (picklable under Windows spawn) trivial worker."""
    if task.get("boom"):
        raise ValueError(task["boom"])
    return {"n": task["n"], "sq": task["n"] * task["n"]}


def test_parallel_results_in_input_order_and_isolate_errors():
    tasks = [{"n": 1}, {"n": 2, "boom": "bad candidate"}, {"n": 3}]
    res = evaluate_parallel(tasks, _demo_worker, max_workers=2)
    assert res[0] == {"n": 1, "sq": 1}
    assert "error" in res[1] and "bad candidate" in res[1]["error"]
    assert res[2] == {"n": 3, "sq": 9}


def test_parallel_serial_path_when_one_worker():
    res = evaluate_parallel([{"n": 4}, {"n": 5}], _demo_worker, max_workers=1)
    assert [r["sq"] for r in res] == [16, 25]


def test_parallel_single_task_short_circuits():
    res = evaluate_parallel([{"n": 7}], _demo_worker, max_workers=4)
    assert res == [{"n": 7, "sq": 49}]


def test_pareto_schema_has_max_workers():
    op = next(t for t in TOOL_SCHEMAS if t["name"] == "optimize_pareto")
    assert "max_workers" in op["parameters"]["properties"]


def _ngspice_works():
    try:
        from pyspice_openfoam_agent.spice.runner import run_transient

        run_transient("* smoke\nV1 a 0 DC 1\nR1 a 0 1k\n.tran 1u 2u\n.end\n",
                      fsw=1e6, n_cycles=2, points_per_cycle=10)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ngspice_works(), reason="ngspice unavailable")
def test_parallel_finalist_verification_end_to_end(tmp_path):
    """Real parallel run: 2 finalists in 2 worker processes, each with its
    own ngspice instance (the shared-instance constraint is why processes).
    On the host the CHT stage fails with the honest container-only reason —
    the SPICE stage still proves the child pipelines ran to completion."""
    import time

    ctx = ToolContext(run_dir=tmp_path / "d", library=load_library())
    r = dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05))
    assert r.ok
    t0 = time.time()
    r2 = dispatch(ctx, "optimize_pareto", dict(
        pop_size=12, n_gen=4, finalists=2, max_workers=2))
    assert r2.ok, r2.payload
    finalists = r2.payload["finalists"]
    assert len(finalists) == 2
    assert ctx.artifacts["pareto"]["max_workers"] == 2
    for f in finalists:
        # host: SPICE completed in the child, CHT needs the container
        assert f["verified"] is False
        assert "CHT" in f["reason"]
    # isolated per-finalist artifacts on disk
    for n in (1, 2):
        assert (tmp_path / "d" / f"pareto_{n}").is_dir()
    # parallel wall-clock must beat 2x the serial single-finalist time budget
    # loosely: the whole tool call stayed well under 2 minutes
    assert time.time() - t0 < 120
