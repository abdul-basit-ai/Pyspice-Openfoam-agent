"""Tests for the Phase 6 / electro-thermal orchestrator tools (goals wiring)."""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.orchestrator.tools import (
    ToolContext, dispatch,
)


@pytest.fixture()
def ctx(tmp_path):
    from pyspice_openfoam_agent.library.loader import load_library
    return ToolContext(run_dir=tmp_path, library=load_library())


def _sized_selected(ctx):
    """Drive size_converter + select_components so L/C/ESR exist in context."""
    r = dispatch(ctx, "size_converter", {
        "Vin": 12, "Vout": 5, "Iout": 5, "fsw_khz": 500, "Vripple": 0.05})
    assert r.ok, r.payload
    r = dispatch(ctx, "select_components", {})
    assert r.ok, f"select failed: {r.payload}"
    return r


def test_tool_analyze_control_loop_ok(ctx):
    _sized_selected(ctx)
    r = dispatch(ctx, "analyze_control_loop", {})
    assert r.ok, r.payload
    assert "phase_margin_deg" in r.payload
    assert r.payload["passed"] is True, r.payload
    assert "control_loop" in ctx.artifacts


def test_tool_control_loop_requires_selection(ctx):
    r = dispatch(ctx, "analyze_control_loop", {})
    assert not r.ok
    assert "select_components" in r.payload["error"]


def test_tool_electro_thermal_ok(ctx):
    _sized_selected(ctx)
    r = dispatch(ctx, "electro_thermal_converge", {"v_in_m_s": 1.5})
    assert r.ok, r.payload
    assert r.payload["converged"] is True, r.payload
    assert 25.0 < r.payload["final_tj_C"] < 150.0


def test_tool_electro_thermal_requires_selection(ctx):
    r = dispatch(ctx, "electro_thermal_converge", {})
    assert not r.ok
    assert "select" in r.payload["error"]


def test_unknown_tool(ctx):
    r = dispatch(ctx, "no_such_tool", {})
    assert not r.ok
    assert "unknown tool" in r.payload["error"]