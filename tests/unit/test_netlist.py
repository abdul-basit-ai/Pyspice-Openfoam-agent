"""Phase 3 standalone checkpoint tests.

The plan's checkpoint: feed the sizing engine's output from Phase 2's
validated worked example (Vin=12, Vout=5, Iout=5A, fsw=500kHz) through
selection + netlist generation, confirm the generated .cir parses cleanly in
ngspice with zero syntax errors.

Requires a real `ngspice` binary on PATH for the parse-check assertions;
those are skipped (not failed) if it isn't installed, so this file still
documents/exercises the selection logic in CI environments without ngspice.
"""

from __future__ import annotations

import shutil

import pytest

from pyspice_openfoam_agent.library.loader import Library, load_library
from pyspice_openfoam_agent.netlist.builder import NetlistLintError, build_and_write, build_netlist
from pyspice_openfoam_agent.netlist.selector import SelectionError, select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size

NGSPICE_AVAILABLE = shutil.which("ngspice") is not None
_skip_no_ngspice = pytest.mark.skipif(not NGSPICE_AVAILABLE, reason="ngspice not installed")

PUBLISHED = dict(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.05)


@pytest.fixture(scope="module")
def lib() -> Library:
    return load_library()


# ---------- the checkpoint itself ----------


@_skip_no_ngspice
def test_checkpoint_buck_netlist_parses_cleanly(lib: Library, tmp_path) -> None:
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    path, result = build_and_write(spec, sizing, sel, tmp_path / "buck.cir")
    assert path.exists()
    assert result.ok, f"ngspice reported errors:\n{result.log}"


@_skip_no_ngspice
@pytest.mark.parametrize(
    "spec_kwargs",
    [
        dict(Vin=5.0, Vout=12.0, Iout=2.0, fsw=900e3, Vripple=0.05),  # boost
        dict(Vin=5.0, Vout=5.0, Iout=2.0, fsw=900e3, Vripple=0.05),  # buck_boost
    ],
)
def test_boost_and_buck_boost_netlists_parse_cleanly(lib: Library, spec_kwargs, tmp_path) -> None:
    spec = Spec(**spec_kwargs)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    path, result = build_and_write(spec, sizing, sel, tmp_path / f"{sizing.topology}.cir")
    assert result.ok, f"ngspice reported errors:\n{result.log}"


# ---------- component selection ----------


def test_selection_respects_margins(lib: Library) -> None:
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    assert sel.inductor.L >= sizing.L_min * 1.20 - 1e-15
    assert sel.capacitor.C >= sizing.C_min * 1.20 - 1e-15
    assert sel.mosfet.Vds_max >= spec.Vin * 1.20 - 1e-9
    assert sel.mosfet.Id_max >= sizing.I_peak * 1.30 - 1e-9


def test_selection_picks_lowest_rds_on_that_qualifies(lib: Library) -> None:
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    # Hand-checked against mosfets.yaml: BSC014N04LS (1.4 mOhm, 40V, 100A) is
    # the lowest-Rds_on part that clears Vin=12V*1.2=14.4V and I_peak*1.3.
    assert sel.mosfet.part_number == "BSC014N04LS"


def test_selection_raises_when_library_cannot_satisfy_spec(lib: Library) -> None:
    # Passes Phase 2's precheck (power stays under 500 W) but 90 A peak
    # current is beyond any library MOSFET's Id_max*margin (max is 100 A).
    spec = Spec(Vin=3.3, Vout=1.8, Iout=90.0, fsw=300e3, Vripple=0.05)
    sizing = size(spec)
    with pytest.raises(SelectionError):
        select_components(lib, spec, sizing)


def test_capacitor_esr_ripple_note_fires_when_relevant(lib: Library) -> None:
    # Tighter Vripple budget than the published example, with the same
    # ripple current -- the lowest-ESR qualifying part's ESR-driven ripple
    # alone exceeds this budget, which must surface as a note, not a raise.
    spec = Spec(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.01)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    assert any("ESR-driven ripple" in n for n in sel.notes)


# ---------- netlist text sanity (no ngspice required) ----------


def test_build_netlist_is_deterministic_and_topology_tagged(lib: Library) -> None:
    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    text1 = build_netlist(spec, sizing, sel)
    text2 = build_netlist(spec, sizing, sel)
    assert text1 == text2
    assert sel.mosfet.part_number in text1
    assert sel.inductor.part_number in text1
    assert sel.capacitor.part_number in text1
    assert text1.strip().endswith(".end")


def test_lint_error_wraps_missing_ngspice_binary(lib: Library, tmp_path) -> None:
    from pyspice_openfoam_agent.netlist.builder import lint_netlist, write_netlist

    spec = Spec(**PUBLISHED)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    path = write_netlist(build_netlist(spec, sizing, sel), tmp_path / "buck.cir")
    with pytest.raises(NetlistLintError):
        lint_netlist(path, ngspice_bin="definitely-not-a-real-binary")