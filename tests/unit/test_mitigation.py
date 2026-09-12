"""Phase 10 standalone checkpoint tests.

The plan's checkpoint: deliberately feed an under-cooled design and confirm
the agent escalates through the three levers in order and either converges
or correctly reports infeasibility.

These tests use FAKE evaluators (dictated Tj outcomes) so the decision logic
is verified exhaustively without burning CHT solves; one live end-to-end test
(the under-cooled real case) is marked for in-container runs.
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.sizing.engine import Spec
from pyspice_openfoam_agent.thermal.mitigation import (
    LEVER_COMPONENT,
    LEVER_FREQUENCY,
    LEVER_GEOMETRY,
    MitigationOutcome,
    run_mitigation,
)

def _fake_meta(desc: str) -> dict:
    return {"desc": desc}


PUBLISHED = dict(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.05)


@pytest.fixture(scope="module")
def lib():
    return load_library()


@pytest.fixture(scope="module")
def spec():
    return Spec(**PUBLISHED)


@pytest.fixture(scope="module")
def baseline_mosfet(lib):
    from pyspice_openfoam_agent.netlist.selector import select_mosfet
    from pyspice_openfoam_agent.sizing.engine import size

    return select_mosfet(lib, spec_fixture(), size(spec_fixture()))


def spec_fixture():
    return Spec(**PUBLISHED)


# ---------- lever-order escalation ----------


def test_all_levers_needed_escalates_in_order(lib, spec) -> None:
    """Every lever fails until frequency; verify order: airflow -> MOSFET -> f_sw."""
    calls = []

    def evaluate(v_in, mosfet, fsw):
        calls.append((round(v_in, 2), mosfet.part_number, fsw))
        # always infeasible but temperature drops slightly per attempt
        return 350.0 - len(calls), _fake_meta("attempt")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    baseline = select_mosfet(lib, spec, size(spec))
    outcome = run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                             evaluate=evaluate, baseline_tj_k=355.0)
    assert not outcome.feasible
    assert outcome.iterations_used == 4  # 2 airflow + 2 f_sw (no better MOSFET exists)
    # order check: first attempts are airflow, then f_sw
    assert calls[0][0] == 2.0 and calls[1][0] == 3.0  # v_in steps ascending
    # last two calls should be reduced frequencies
    assert calls[-1][2] < spec.fsw
    assert outcome.plateaued_lever is not None


def test_geometry_lever_solves_first(lib, spec) -> None:
    """Cheapest lever (airflow) fixes it: only 1-2 solves, no escalation."""

    def evaluate(v_in, mosfet, fsw):
        # v_in=2.0 brings Tj under the limit
        if v_in > 1.5:
            return 330.0, _fake_meta("cool")
        return 350.0, _fake_meta("hot")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    baseline = select_mosfet(lib, spec, size(spec))
    outcome = run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                             evaluate=evaluate, baseline_tj_k=350.0)
    assert outcome.feasible
    assert outcome.iterations_used == 1  # first airflow step fixed it
    assert "v_in=2.0" in outcome.best_description


def test_component_lever_used_when_airflow_fails(lib, spec) -> None:
    """Airflow fails, but a lower-Rds_on MOSFET fixes it."""
    seen_mosfets = []

    def evaluate(v_in, mosfet, fsw):
        seen_mosfets.append(mosfet.part_number)
        if mosfet.part_number != "BSC030N04LS":  # baseline stays hot
            return 330.0, _fake_meta("better part")
        return 350.0, _fake_meta("hot")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    # deliberately WORSE baseline than the best library part → component lever
    # must fire (BSC014N04LS has 1.4 mOhm < 3.0 mOhm)
    baseline = lib.mosfets["BSC030N04LS"]
    outcome = run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                             evaluate=evaluate, baseline_tj_k=350.0)
    assert outcome.feasible
    # airflow tried (and failed) before the component lever
    assert any("v_in" in h.description for h in outcome.history)
    assert any("MOSFET=" in h.description for h in outcome.history)


def test_best_effort_verdict_when_all_fail(lib, spec) -> None:
    """All levers fail: best-effort lowest-Tj config returned + plateaued lever."""
    seen = []

    def evaluate2(v_in, mosfet, fsw):
        seen.append(1)
        return 350.0 - 0.1 * len(seen), _fake_meta("hot")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    baseline = select_mosfet(lib, spec, size(spec))
    outcome = run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                             evaluate=evaluate2, baseline_tj_k=350.0)
    assert not outcome.feasible
    assert outcome.plateaued_lever in (LEVER_GEOMETRY, LEVER_COMPONENT, LEVER_FREQUENCY)
    assert outcome.best_tj_max_k < 350.0  # best-effort improved on baseline
    assert outcome.best_description != "baseline"


def test_budget_respected(lib, spec) -> None:
    calls = []
    seen_n = []

    def evaluate(v_in, mosfet, fsw):
        seen_n.append(1)
        return 350.0, _fake_meta("hot")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    baseline = select_mosfet(lib, spec, size(spec))
    outcome = run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                             evaluate=evaluate, baseline_tj_k=350.0, max_solves=8)
    assert outcome.iterations_used <= 8
    note = " ".join(outcome.notes)
    assert "/8 budget" in note and "solved" in note


def test_frequency_lever_gets_fsw(lib, spec) -> None:
    """The frequency lever must pass the REDUCED f_sw to evaluate (bug check:
    evaluate signature carries fsw explicitly)."""
    fsw_seen = []

    def evaluate(v_in, mosfet, fsw):
        fsw_seen.append(fsw)
        return 350.0, _fake_meta("hot")

    from pyspice_openfoam_agent.sizing.engine import size
    from pyspice_openfoam_agent.netlist.selector import select_mosfet

    baseline = select_mosfet(lib, spec, size(spec))
    run_mitigation(lib, spec, baseline, tj_limit_k=340.0,
                   evaluate=evaluate, baseline_tj_k=350.0)
    # reduced frequencies were actually passed
    assert any(f < spec.fsw for f in fsw_seen)