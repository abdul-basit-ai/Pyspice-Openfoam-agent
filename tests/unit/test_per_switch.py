"""Per-switch component support (remain_plan follow-up): the builder must
emit each switch position's OWN Ron/Qg instead of one part for the whole
circuit, while keeping single-part designs byte-identical to the old
behavior.

Real-world driver: buck-boost boards routinely use a higher-voltage part on
one leg and a lower-Rds part on the other (LM5175EVM-HD: BSZ042N06NS on
QH1/QL1, BSZ0902NS on QH2/QL2). The validation injector needs exactly this.
"""

from __future__ import annotations

import re

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.netlist.builder import build_netlist
from pyspice_openfoam_agent.netlist.selector import SelectedComponents
from pyspice_openfoam_agent.sizing.engine import Spec, size


def _parts():
    lib = load_library()
    return lib.mosfets["CSD16415Q5"], lib.mosfets["CSD16321Q5"]


def _bb_selection():
    hs, ls = _parts()
    spec = Spec(Vin=12, Vout=12, Iout=2, fsw=400e3, Vripple=0.1,
                topology_constraint="buck_boost")
    sizing = size(spec)
    sel = SelectedComponents(mosfet=hs, mosfet_second=ls,
                             inductor=lib_inductor(), capacitor=lib_capacitor(),
                             notes=["test: two-leg injection"])
    return spec, sizing, sel


def lib_inductor():
    return load_library().inductors["XAL1010-472ME"]


def lib_capacitor():
    return load_library().capacitors["GRM31CR61E476ME15"]


# ---------------- buck-boost: two distinct leg parts ----------------

def test_buck_boost_emits_both_ron_values():
    spec, sizing, sel = _bb_selection()
    net = build_netlist(spec, sizing, sel)
    # leg-1 (HS + LS_B) carries the primary part's Ron; leg-2 (LS_A + SYNC)
    # carries the second part's Ron — two DISTINCT model values present
    ron_hs = re.search(r"\.model HS_MOD SW\(Ron=([\d.e-]+)", net)
    ron_lsa = re.search(r"\.model LS_A_MOD SW\(Ron=([\d.e-]+)", net)
    assert ron_hs and ron_lsa, net
    assert float(ron_hs.group(1)) == pytest.approx(sel.mosfet.Rds_on)
    assert float(ron_lsa.group(1)) == pytest.approx(sel.mosfet_second.Rds_on)
    assert ron_hs.group(1) != ron_lsa.group(1)


def test_buck_boost_switch_instances_reference_their_leg_model():
    spec, sizing, sel = _bb_selection()
    net = build_netlist(spec, sizing, sel)
    # device lines: HS + SYNC sit on the legs their model names claim
    assert re.search(r"^Shs in sw_a gate_hs 0 HS_MOD$", net, re.M)
    assert re.search(r"^Sls_a sw_a 0 gate_ls_a 0 LS_A_MOD$", net, re.M)
    assert re.search(r"^Ssync sw_b out gate_sync 0 SYNC_MOD$", net, re.M)
    assert re.search(r"^Sls_b sw_b 0 gate_ls_b 0 LS_B_MOD$", net, re.M)
    # and the header names both parts (no silent single-part substitution)
    assert sel.mosfet.part_number in net
    assert sel.mosfet_second.part_number in net


# ---------------- backward compatibility ----------------

def test_single_part_design_unchanged():
    """No mosfet_second -> every switch model carries the primary part's Ron
    (the historical single-part behavior, byte-for-byte on the .model lines)."""
    primary, _ = _parts()
    spec = Spec(Vin=12, Vout=12, Iout=2, fsw=400e3, Vripple=0.1,
                topology_constraint="buck_boost")
    sizing = size(spec)
    sel = SelectedComponents(mosfet=primary, inductor=lib_inductor(),
                             capacitor=lib_capacitor())
    net = build_netlist(spec, sizing, sel)
    rons = re.findall(r"\.model \w+_MOD SW\(Ron=([\d.e-]+)", net)
    assert len(rons) == 4
    assert all(float(r) == pytest.approx(primary.Rds_on) for r in rons)


def test_buck_hs_ls_positions():
    """Buck: mosfet = HS position, mosfet_second = LS position."""
    hs, ls = _parts()
    spec = Spec(Vin=12, Vout=5, Iout=2, fsw=500e3, Vripple=0.05)
    sizing = size(spec)
    sel = SelectedComponents(mosfet=hs, mosfet_second=ls,
                             inductor=lib_inductor(), capacitor=lib_capacitor())
    net = build_netlist(spec, sizing, sel)
    ron_hs = re.search(r"\.model HS_MOD SW\(Ron=([\d.e-]+)", net)
    ron_ls = re.search(r"\.model LS_MOD SW\(Ron=([\d.e-]+)", net)
    assert float(ron_hs.group(1)) == pytest.approx(hs.Rds_on)
    assert float(ron_ls.group(1)) == pytest.approx(ls.Rds_on)
