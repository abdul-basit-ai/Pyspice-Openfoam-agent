"""Pytest entry for the Phase 13 regression suite (wraps regression.py)."""
from __future__ import annotations

import shutil

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.builder import build_and_write
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size
from tests.reference.regression import REFERENCES


@pytest.fixture(scope="module")
def lib():
    return load_library()


@pytest.mark.parametrize("ref", REFERENCES, ids=[r.name for r in REFERENCES])
@pytest.mark.skipif(shutil.which("ngspice") is None,
                    reason="ngspice binary not on PATH (lint stage requires it)")
def test_reference_design(lib, ref, tmp_path):
    spec = Spec(**ref.spec_kwargs)
    sizing = size(spec)
    assert sizing.topology == ref.expect["topology"], ref.name
    if "D" in ref.expect:
        assert sizing.D == pytest.approx(ref.expect["D"], abs=2e-3), ref.name
    if "L_min_uH" in ref.expect:
        assert sizing.L_min * 1e6 == pytest.approx(ref.expect["L_min_uH"], rel=0.01), ref.name
    if "I_peak_A" in ref.expect:
        assert sizing.I_peak == pytest.approx(ref.expect["I_peak_A"], abs=0.05), ref.name
    sel = select_components(lib, spec, sizing)
    path, lint = build_and_write(spec, sizing, sel, tmp_path / f"{ref.name}.cir", lint=True)
    assert path.exists(), ref.name
    if lint is not None:
        assert lint.ok, f"{ref.name}: ngspice lint failed:\n{lint.log}"