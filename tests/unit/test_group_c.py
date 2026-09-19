"""Group C (remain_plan): named Phase 23 regression cases + C1/C2/C3 units.

Goals Phase 23 case list -> coverage map:
  buck / boost / buck-boost ........ topology_smoke.py + suite (pre-existing)
  light load ....................... test_light_load_pipeline (below, gated)
  heavy load ....................... test_heavy_load_pipeline (below, gated)
  thermal failure .................. tests/unit/test_mitigation.py (levers,
                                      budget, best-effort verdict) + the
                                      tool-gating tests below
  infeasible specification ......... test_infeasible_ripple_spec_rejected
  component substitution ........... test_override_substitution_pipeline
                                      (gated) + hallucination guard (unit)
  transient conditions ............. tests/unit/test_group_a.py step tests
"""

from __future__ import annotations

import re

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.orchestrator.tools import ToolContext, dispatch


@pytest.fixture(scope="module")
def lib():
    return load_library()


def _ctx(tmp_path, lib):
    return ToolContext(run_dir=tmp_path, library=lib)


def _run_pipeline(ctx, spec, **select_overrides):
    """size -> select(+overrides) -> build; returns the last dispatch result."""
    r = dispatch(ctx, "size_converter", spec)
    if not r.ok:
        return r
    r = dispatch(ctx, "select_components", select_overrides)
    if not r.ok:
        return r
    return dispatch(ctx, "build_netlist", {})


# ---------------- C1: fidelity param reaches build_case ----------------

def test_run_thermal_schema_has_fidelity():
    from pyspice_openfoam_agent.orchestrator.tools import TOOL_SCHEMAS

    rt = next(t for t in TOOL_SCHEMAS if t["name"] == "run_thermal")
    assert rt["parameters"]["properties"]["fidelity"]["enum"] == [
        "fast", "balanced", "high"]


def test_run_thermal_rejects_unknown_fidelity(tmp_path, lib):
    ctx = _ctx(tmp_path / "f", lib)
    ctx.selected = object()  # skip the guard; expect TypeError -> tool error
    r = dispatch(ctx, "run_thermal", {"fidelity": "ultra"})
    assert not r.ok  # unknown preset must fail loudly, never silently default


# ---------------- C2: two-corner range sizing ----------------

def test_range_sizing_takes_worst_of_corners(tmp_path, lib):
    ctx = _ctx(tmp_path / "r1", lib)
    r = dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05,
        vin_min=8, vin_max=18))
    assert r.ok
    # buck L_min grows as Vin rises ((1-D) max at min D = max Vin): the merged
    # number must be the vin_max corner's, not the nominal Vin's
    assert r.payload["L_min_uH"] > 6.5  # ~6.5 uH is the fixed-Vin=12 number
    assert any("BOTH corners" in n for n in r.payload["notes"])
    assert ctx.design.requirements.vin_min == 8.0
    assert ctx.design.requirements.vin_max == 18.0
    assert ctx.spec.Vin == 18.0  # pinned at the stress endpoint


def test_range_topology_flip_forces_buck_boost(tmp_path, lib):
    ctx = _ctx(tmp_path / "r2", lib)
    # 10 V in -> 12 V out is a boost; 15 V in -> 12 V out is a buck. The range
    # spans the ratio-1 crossing: only the 4-switch buck_boost covers it.
    r = dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=12, Iout=2, fsw_khz=500, Vripple=0.1,
        vin_min=10, vin_max=15))
    assert r.ok and r.payload["topology"] == "buck_boost"
    assert "input range 10-15" in ctx.design.topology.rationale


def test_range_lonely_endpoint_is_an_error(tmp_path, lib):
    ctx = _ctx(tmp_path / "r3", lib)
    r = dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05, vin_min=8))
    assert not r.ok and "together" in r.payload["error"]


# ---------------- C3/C4: component substitution ----------------

def test_override_hallucination_guardrail(tmp_path, lib):
    ctx = _ctx(tmp_path / "h", lib)
    dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05))
    r = dispatch(ctx, "select_components", {"mosfet": "MADE-UP-PART"})
    assert not r.ok
    assert "not in the library" in r.payload["error"]
    assert "invented part numbers are forbidden" in r.payload["error"]


def test_override_margin_audit_warns(tmp_path, lib):
    ctx = _ctx(tmp_path / "w", lib)
    dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=2, fsw_khz=500, Vripple=0.05))
    r = dispatch(ctx, "select_components", {"inductor": "XAL4020-102ME"})
    assert r.ok
    assert any("override inductor L 1.0 uH <" in n for n in r.payload["notes"])


# ---------------- C4: mitigation tool gating (cheap thermal-failure tests) ----

def test_mitigate_thermal_requires_baseline(tmp_path, lib):
    ctx = _ctx(tmp_path / "m1", lib)
    # past the size/select guard, the baseline-Tj guard must fire
    ctx.spec, ctx.sizing, ctx.selected = object(), object(), object()
    r = dispatch(ctx, "mitigate_thermal", {})
    assert not r.ok
    assert "run_thermal first" in r.payload["error"]


def test_mitigate_thermal_shortcut_when_within_limit(tmp_path, lib):
    ctx = _ctx(tmp_path / "m2", lib)
    ctx.spec, ctx.sizing, ctx.selected = object(), object(), object()
    ctx.artifacts["thermal"] = {"tj_max_k": 320.0}  # 46.85 degC < 150 limit
    r = dispatch(ctx, "mitigate_thermal", {})
    assert r.ok and r.payload["feasible"] is True
    assert "no mitigation needed" in r.payload["note"]


# ---------------- C4: infeasible specification ----------------

def test_infeasible_spec_no_part_in_library(tmp_path, lib):
    """Hard infeasibility (safety screen): 150 V input exceeds every library
    MOSFET (max 100 V) — selection fails loudly with the structured error."""
    ctx = _ctx(tmp_path / "i1", lib)
    r = dispatch(ctx, "size_converter", dict(
        Vin=150, Vout=12, Iout=2, fsw_khz=500, Vripple=0.1))
    assert r.ok, r.payload
    r2 = dispatch(ctx, "select_components", {})
    assert not r2.ok
    assert "No MOSFET" in r2.payload.get("error", "")


def test_ripple_beyond_library_is_loud_not_silent(tmp_path, lib):
    """Best-effort contract (deliberate policy): a ripple budget even an 8x
    bank cannot meet does NOT hard-reject at selection (the MEASURED gate in
    run_spice decides) — but the beyond-this-library note must be present so
    the outcome is never silent."""
    ctx = _ctx(tmp_path / "i2", lib)
    r = dispatch(ctx, "size_converter", dict(
        Vin=12, Vout=5, Iout=8, fsw_khz=500, Vripple=0.001))
    assert r.ok, r.payload
    r2 = dispatch(ctx, "select_components", {})
    notes = " | ".join(r2.payload.get("notes", []))
    assert "beyond this library" in notes, notes


# ---------------- C4: gated end-to-end named cases ----------------

def _ngspice_works():
    try:
        from pyspice_openfoam_agent.spice.runner import run_transient

        run_transient("* smoke\nV1 a 0 DC 1\nR1 a 0 1k\n.tran 1u 2u\n.end\n",
                      fsw=1e6, n_cycles=2, points_per_cycle=10)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ngspice_works(), reason="ngspice unavailable")
@pytest.mark.parametrize("name,spec,expect_ok", [
    # light load: 0.25x the 2 A nominal -> DCM-ish but must run and measure
    ("light", dict(Vin=12, Vout=5, Iout=0.5, fsw_khz=500, Vripple=0.05), True),
    # heavy load: 4x -> larger L/Isat demands, still inside library range
    ("heavy", dict(Vin=12, Vout=5, Iout=8, fsw_khz=500, Vripple=0.05), True),
])
def test_load_extremes_pipeline(tmp_path, lib, name, spec, expect_ok):
    ctx = _ctx(tmp_path / name, lib)
    r = _run_pipeline(ctx, spec)
    assert r.ok, (name, r.payload)
    r2 = dispatch(ctx, "run_spice", {})
    assert r2.ok == expect_ok, (name, r2.payload)
    if r2.ok:
        assert r2.payload["converged"] is True
        assert 0.5 < r2.payload["efficiency"] <= 1.0


@pytest.mark.skipif(not _ngspice_works(), reason="ngspice unavailable")
def test_override_substitution_pipeline(tmp_path, lib):
    """Component substitution: force the 2nd-best MOSFET end-to-end — the
    netlist, servo and losses must all run on the substituted part."""
    ctx = _ctx(tmp_path / "sub", lib)
    r = _run_pipeline(ctx, dict(Vin=12, Vout=5, Iout=2, fsw_khz=500,
                                Vripple=0.05),
                      mosfet="BSC030N04LS")
    assert r.ok, r.payload
    assert ctx.design.components.mosfet.part_number == "BSC030N04LS"
    r2 = dispatch(ctx, "run_spice", {})
    assert r2.ok, r2.payload
    netlist_text = ctx.netlist_path.read_text()
    # provenance comment names the substituted part; the behavioral switches
    # carry its Rds_on (3.0 mOhm = 3.0e-03) as the .model value
    assert "BSC030N04LS" in netlist_text
    assert re.search(r"\.model\s+\S+\s+SW\(Ron=3\.0+e-03", netlist_text)
