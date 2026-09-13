"""Phase 15 tests: run-state bridge + visualization modules (headless-safe)."""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.ui.run_state import RunState


# ---------- RunState round-trip ----------


def test_run_state_lifecycle(tmp_path) -> None:
    rs = RunState(tmp_path / "run_x")
    rs.init({"Vin": 12}, max_steps=6)
    s = rs.read()
    assert s["status"] == "running"
    assert s["max_steps"] == 6
    rs.log_tool("size_converter", True, {"topology": "buck"},
                {"sizing": {"topology": "buck"}}, step=1)
    s = rs.read()
    assert len(s["history"]) == 1
    assert s["history"][0]["tool"] == "size_converter"
    assert s["artifacts"]["sizing"]["topology"] == "buck"
    rs.finish({"summary": "done"})
    s = rs.read()
    assert s["status"] == "done"
    assert s["final"]["summary"] == "done"


def test_run_state_error_finish(tmp_path) -> None:
    rs = RunState(tmp_path / "run_err")
    rs.init({}, 3)
    rs.finish(None, error="boom")
    s = rs.read()
    assert s["status"] == "error"
    assert s["error"] == "boom"


def test_run_state_read_missing(tmp_path) -> None:
    rs = RunState(tmp_path / "run_missing")
    assert rs.read() is None


def test_run_state_survives_corrupt_file(tmp_path) -> None:
    rs = RunState(tmp_path / "run_corrupt")
    rs.path.write_text("{not json")
    assert rs.read() is None  # degrade, don't crash


def test_run_state_log_compacts_large_values(tmp_path) -> None:
    rs = RunState(tmp_path / "run_big")
    rs.init({}, 2)
    big = {"waveform": list(range(10000)), "note": "x" * 500}
    rs.log_tool("run_spice", True, big, {}, 1)
    s = rs.read()
    import os

    # state file must stay small (compaction worked)
    assert os.path.getsize(rs.path) < 100_000  # not 10000-item arrays


# ---------- visualize: schematic + waveforms (matplotlib Agg, headless-safe) ----------


def test_schematic_renders_buck(tmp_path) -> None:
    schemdraw = pytest.importorskip("schemdraw")
    from pyspice_openfoam_agent.ui.visualize import draw_schematic

    out = draw_schematic("buck", "BSC014N04LS", "XAL1010-472ME", 4.7,
                         "GRM31CR61E476ME15", 47.0, tmp_path / "buck.png",
                         vin=12.0, vout=5.0)
    assert out.exists() and out.stat().st_size > 1000


def test_schematic_renders_boost_and_buckboost(tmp_path) -> None:
    pytest.importorskip("schemdraw")
    from pyspice_openfoam_agent.ui.visualize import draw_schematic

    for topo in ("boost", "buck_boost"):
        out = draw_schematic(topo, "BSC014N04LS", "XAL5030-222ME", 2.2,
                             "EEF-CD1D101R", 100.0, tmp_path / f"{topo}.png",
                             vin=5.0, vout=12.0 if topo == "boost" else 5.0)
        assert out.exists()


def test_waveform_plot_renders(tmp_path) -> None:
    import numpy as np

    pytest.importorskip("matplotlib")
    from pyspice_openfoam_agent.spice.runner import TransientResult
    from pyspice_openfoam_agent.ui.visualize import plot_waveforms

    t = np.linspace(0, 400e-6, 4000)
    v = 5 * (1 - np.exp(-t / 100e-6)) + 0.01 * np.sin(2 * np.pi * t / 2e-6)
    res = TransientResult(vectors={"time": t, "out": v})
    out = plot_waveforms(res, tmp_path / "wf.png", vout_target=5.0)
    assert out.exists() and out.stat().st_size > 1000
