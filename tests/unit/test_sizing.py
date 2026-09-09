"""Phase 2 standalone checkpoint: sizing engine vs the published worked example.

Anchor (independently published, PCBSync buck design guide, verified by us):
  Spec:  Vin=12V, Vout=5V, Iout=5A, fsw=500kHz, ripple 40% of Iout
  Published: D = 0.417, L = 2.92 uH, I_peak = 6 A
Our engine must reproduce these within 1% (their 2.92 is a rounding of 2.917).
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.sizing.engine import FeasibilityError, Spec, classify_topology, precheck, size

PUBLISHED = {
    "Vin": 12.0,
    "Vout": 5.0,
    "Iout": 5.0,
    "fsw": 500e3,
    "ripple_ratio": 0.40,
    "Vripple": 0.05,  # 50 mV — arbitrary but fixed; only C_min depends on it
}


@pytest.fixture(scope="module")
def result():
    return size(Spec(**PUBLISHED))


# ---------- the checkpoint itself ----------


def test_checkpoint_buck_duty(result) -> None:
    assert result.topology == "buck"
    assert abs(result.D - 0.4167) < 0.001  # published 0.417


def test_checkpoint_buck_L_min(result) -> None:
    assert abs(result.L_min - 2.917e-6) < 0.01e-6  # published 2.92 uH
    assert abs(result.L_min - 2.92e-6) / 2.92e-6 < 0.01  # within 1%


def test_checkpoint_buck_I_peak(result) -> None:
    assert abs(result.I_peak - 6.0) < 1e-9  # published exactly 6 A


# ---------- topology classification ----------


def test_classify() -> None:
    assert classify_topology(Spec(**{**PUBLISHED, "topology_constraint": None})) == "buck"
    assert classify_topology(Spec(Vin=5, Vout=12, Iout=2, fsw=300e3, Vripple=0.05)) == "boost"
    assert classify_topology(Spec(Vin=5, Vout=5, Iout=2, fsw=300e3, Vripple=0.05)) == "buck_boost"
    # Explicit constraint wins even when ratio disagrees
    assert (
        classify_topology(Spec(**{**PUBLISHED, "topology_constraint": "boost"})) == "boost"
    )


# ---------- pre-check gates ----------


def test_precheck_rejects_impossible() -> None:
    with pytest.raises(Exception):  # pydantic ValidationError at Spec level
        Spec(Vin=-12, Vout=5, Iout=5, fsw=500e3, Vripple=0.05)
    with pytest.raises(FeasibilityError, match="ripple"):
        size(Spec(Vin=12, Vout=5, Iout=5, fsw=500e3, Vripple=10.0))  # ripple >= Vout
    with pytest.raises(FeasibilityError, match="500 W"):
        size(Spec(Vin=48, Vout=12, Iout=20, fsw=300e3, Vripple=0.05))  # 960 W in
    with pytest.raises(Exception):  # fsw band enforced in Spec validator
        Spec(Vin=12, Vout=5, Iout=5, fsw=2e6, Vripple=0.05)


def test_precheck_passes_valid() -> None:
    pc = precheck(Spec(**PUBLISHED))
    assert pc.feasible and pc.reasons == []


# ---------- topology-specific feasibility ----------


def test_buck_rejects_step_up() -> None:
    with pytest.raises(FeasibilityError, match="buck requires"):
        size(Spec(Vin=5, Vout=12, Iout=2, fsw=300e3, Vripple=0.05, topology_constraint="buck"))


def test_boost_rejects_step_down() -> None:
    with pytest.raises(FeasibilityError, match="boost requires"):
        size(Spec(**{**PUBLISHED, "topology_constraint": "boost"}))


# ---------- boost / buck-boost math sanity (charge-balance identities) ----------


def test_boost_sizing_consistency() -> None:
    """At L=L_min the ripple equation must return exactly the target di_pp."""
    spec = Spec(Vin=5, Vout=12, Iout=2, fsw=300e3, ripple_ratio=0.3, Vripple=0.05)
    r = size(spec)
    di_from_L = spec.Vin * r.D / (r.L_min * spec.fsw)
    assert abs(di_from_L - r.di_pp) < 1e-9
    # Boost current relation: I_L_avg = Iout/(1-D)
    assert abs(r.I_L_avg - spec.Iout / (1 - r.D)) < 1e-9


def test_buck_boost_duty_identity() -> None:
    spec = Spec(Vin=5, Vout=5, Iout=2, fsw=300e3, Vripple=0.05)
    r = size(spec)
    assert abs(r.D - 0.5) < 1e-9  # symmetric buck-boost -> D=0.5
    assert r.topology == "buck_boost"
