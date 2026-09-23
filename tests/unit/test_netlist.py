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
    # Hand-checked against mosfets.yaml: CSD16415Q5 (1.15 mOhm, 25V, 100A) is
    # the lowest-Rds_on part that clears Vin=12V*1.2=14.4V and I_peak*1.3.
    assert sel.mosfet.part_number == "CSD16415Q5"


def test_selection_raises_when_library_cannot_satisfy_spec(lib: Library) -> None:
    # Passes Phase 2's precheck (power stays under 500 W) but 90 A peak
    # current is beyond any library MOSFET's Id_max*margin (max is 100 A).
    spec = Spec(Vin=3.3, Vout=1.8, Iout=90.0, fsw=300e3, Vripple=0.05)
    sizing = size(spec)
    with pytest.raises(SelectionError):
        select_components(lib, spec, sizing)


def test_capacitor_ripple_budget_handled_by_banking(lib: Library) -> None:
    # Tighter Vripple budget than the published example: the lowest-ESR
    # single part misses it, so the selector must now SOLVE it with an MLCC
    # bank (C adds, ESR divides) instead of only warning.
    spec = Spec(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.01)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    assert any("bank" in n for n in sel.notes)
    assert sel.capacitor.part_number.startswith("2x "), sel.capacitor.part_number


def test_capacitor_esr_ripple_note_fires_when_unmeetable(lib: Library) -> None:
    # A budget even an 8-unit bank cannot meet must surface the ESR-driven
    # note on the returned (best single) part — and screening rejects it.
    spec = Spec(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.0015)
    sizing = size(spec)
    sel = select_components(lib, spec, sizing)
    assert any("beyond this library" in n for n in sel.notes)


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


def test_dead_time_gates_never_overlap() -> None:
    """Synchronous gate drives must have a non-overlap dead-time gap."""
    from pyspice_openfoam_agent.netlist.builder import _switch_pair, _DEAD_TIME_DEFAULT_S
    Tsw = 1e-6  # 1 MHz
    D = 0.5
    pair = _switch_pair("hs", "ls", 0.0014, None, D, Tsw)
    # parse the two PULSE(...) delay + width args: PULSE(V1 V2 Td Tr Tf Pw Per)
    import re
    pulses = re.findall(r"PULSE\(\S+ \S+ (\S+) (\S+) (\S+) (\S+) (\S+)\)", pair)
    assert len(pulses) == 2
    hs = pulses[0]; ls = pulses[1]
    t_hs_off = float(hs[3])           # HS Pw (active width)
    t_ls_on = float(ls[0])             # LS Td (leading delay)
    assert t_ls_on > t_hs_off          # LS turns on AFTER HS turns off
    gap = t_ls_on - t_hs_off
    assert gap >= _DEAD_TIME_DEFAULT_S
    # gate edges are 1ns, dead time 30ns >> 1ns -> real non-overlap
    assert gap > 10e-9


def test_soft_start_ramp_present() -> None:
    """Each builder emits a soft-start PWL on the input rail (>=300*Tsw)."""
    from pyspice_openfoam_agent.library.loader import load_library
    from pyspice_openfoam_agent.sizing.engine import Spec, size
    from pyspice_openfoam_agent.netlist.builder import build_netlist
    from pyspice_openfoam_agent.netlist.selector import select_components

    lib = load_library()
    spec = Spec(Vin=12, Vout=5, Iout=3, fsw=500e3, ripple_ratio=0.4, Vripple=0.05)
    sz = size(spec); sel = select_components(lib, spec, sz)
    cir = build_netlist(spec, sz, sel)
    assert "PWL(0 0" in cir, "soft-start PWL missing"
    assert "DC" in cir.split("PWL(")[0], "input DC value must remain for .op"
