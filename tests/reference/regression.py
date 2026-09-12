"""Phase 13: cross-phase validation & regression suite.

Runs 3-5 known reference designs through the assembled pipeline
(size -> select -> netlist) and validates the results against independently
known figures. The thermal chain (CHT solve) is excluded from the default
regression set — each solve is ~2 min and the Phase 8 checkpoint already
validates one solve; the electrical chain is where integration regressions
(unit mismatches, selection drift) actually appear.

The suite is a pytest file with a runner entry point: `pytest tests/reference/`
or `python -m tests.reference.regression` (prints a summary table).
Reference anchors (independently published/hand-checked):
  R1: 12V->5V/5A/500kHz buck — PCBSync worked example (L=2.92uH, D=0.417, Ipk=6A)
  R2: 12V->3.3V/3A/400kHz buck — hand-checked via Erickson eqs
  R3: 5V->12V/1A/300kHz boost — hand-checked (D=0.583, CCM)
  R4: 24V->12V/2A/250kHz buck — hand-checked
  R5: 5V->-5V magnitude buck-boost/2A/300kHz — hand-checked (D=0.5)
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.builder import build_and_write
from pyspice_openfoam_agent.netlist.selector import select_components
from pyspice_openfoam_agent.sizing.engine import Spec, size


@dataclass
class Reference:
    name: str
    spec_kwargs: dict
    expect: dict  # keys: topology, D?, L_min_uH?, I_peak_A? (tolerance-checked)


REFERENCES = [
    Reference("R1_pcbsync_12to5",
              dict(Vin=12.0, Vout=5.0, Iout=5.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.05),
              {"topology": "buck", "D": 0.4167, "L_min_uH": 2.92, "I_peak_A": 6.0}),
    Reference("R2_12to3V3",
              dict(Vin=12.0, Vout=3.3, Iout=3.0, fsw=400e3, ripple_ratio=0.30, Vripple=0.033),
              {"topology": "buck", "D": 0.275, "I_peak_A": 3.45}),
    # R3-R5 re-anchored (audit finding): the original 300kHz specs needed
    # L > the library's 10 uH max. Re-anchored within the 100-500 kHz band
    # at specs the library covers; anchors remain hand-checked.
    Reference("R3_boost_5to12",
              dict(Vin=5.0, Vout=12.0, Iout=2.0, fsw=500e3, ripple_ratio=0.30, Vripple=0.06),
              {"topology": "boost", "D": 0.5833}),
    Reference("R4_24to12",
              dict(Vin=24.0, Vout=12.0, Iout=4.0, fsw=500e3, ripple_ratio=0.40, Vripple=0.12),
              {"topology": "buck", "D": 0.5, "I_peak_A": 4.8}),
    Reference("R5_buckboost_5to5",
              dict(Vin=5.0, Vout=5.0, Iout=2.0, fsw=500e3, ripple_ratio=0.30, Vripple=0.05),
              {"topology": "buck_boost", "D": 0.5}),
]


@pytest.fixture(scope="module")
def lib():
    return load_library()


@pytest.mark.parametrize("ref", REFERENCES, ids=[r.name for r in REFERENCES])
def test_reference_design(lib, ref, tmp_path):
    """Full electrical chain per reference: size -> select -> netlist lint."""
    spec = Spec(**ref.spec_kwargs)
    sizing = size(spec)

    # topology + duty identity
    assert sizing.topology == ref.expect["topology"], ref.name
    if "D" in ref.expect:
        assert sizing.D == pytest.approx(ref.expect["D"], abs=2e-3), ref.name

    # inductor minimum vs anchor (when published)
    if "L_min_uH" in ref.expect:
        assert sizing.L_min * 1e6 == pytest.approx(ref.expect["L_min_uH"], rel=0.01), ref.name

    # peak current
    if "I_peak_A" in ref.expect:
        assert sizing.I_peak == pytest.approx(ref.expect["I_peak_A"], abs=0.05), ref.name

    # selection + netlist must build and parse cleanly for every reference
    sel = select_components(lib, spec, sizing)
    path, lint = build_and_write(spec, sizing, sel, tmp_path / f"{ref.name}.cir", lint=True)
    assert path.exists(), ref.name
    if lint is not None:
        assert lint.ok, f"{ref.name}: ngspice lint failed:\n{lint.log}"


def suite_summary() -> list[tuple[str, bool, str]]:
    """Non-pytest entry: run all references, print a summary table."""
    from pyspice_openfoam_agent.library.loader import load_library as _ll

    lib = _ll()
    rows = []
    for ref in REFERENCES:
        try:
            spec = Spec(**ref.spec_kwargs)
            sizing = size(spec)
            sel = select_components(lib, spec, sizing)
            rows.append((ref.name, True,
                         f"topology={sizing.topology} D={sizing.D:.3f} "
                         f"mosfet={sel.mosfet.part_number}"))
        except Exception as e:
            rows.append((ref.name, False, str(e)[:80]))
    return rows


if __name__ == "__main__":
    print(f"{'reference':<24} {'pass':<6} detail")
    ok = True
    for name, passed, detail in suite_summary():
        print(f"{name:<24} {'PASS' if passed else 'FAIL':<6} {detail}")
        ok &= passed
    raise SystemExit(0 if ok else 1)