"""Phase 1 standalone checkpoint tests.

The plan's checkpoint: query the library for "MOSFETs rated >= 40V,
Rds_on < 5 mOhm" and confirm correct filtering against a hand-checked answer.
Hand-checked answer (from the YAML, verified against datasheets at entry):
  BSC014N04LS (40V, 1.4 mOhm) and BSC030N04LS (40V, 3.0 mOhm) — exactly 2.
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.library.loader import (
    Library,
    load_library,
    query_capacitors,
    query_inductors,
    query_mosfets,
)
from pyspice_openfoam_agent.library.schema import Capacitor, Inductor, MOSFET


@pytest.fixture(scope="module")
def lib() -> Library:
    return load_library()


# ---------- load & integrity ----------


def test_loads_with_expected_counts(lib: Library) -> None:
    counts = lib.counts()
    assert counts["mosfets"] >= 10
    assert counts["inductors"] >= 10
    assert counts["capacitors"] >= 10


def test_every_part_has_datasheet_provenance(lib: Library) -> None:
    for collection in (lib.mosfets, lib.inductors, lib.capacitors):
        for pn, part in collection.items():
            assert part.datasheet.url.startswith("http"), pn
            assert part.datasheet.page is not None, pn


def test_mosfets_silicon_class_fields(lib: Library) -> None:
    """Phase 5 depends on Qgd/V_plateau/Ciss — must be present and > 0 on all."""
    for m in lib.mosfets.values():
        assert m.Qgd > 0 and m.Qg > 0 and m.Ciss > 0
        assert m.V_plateau > 0
        assert m.package and m.die_x_mm > 0


# ---------- checkpoint: hand-checked query ----------


def test_checkpoint_query_mosfets_40v_5mohm(lib: Library) -> None:
    hits = query_mosfets(lib, Vds_min=40.0, Rds_on_max=5e-3)
    got = [m.part_number for m in hits]
    # Hand-checked: only these two parts satisfy both constraints.
    assert set(got) == {"BSC014N04LS", "BSC030N04LS"}
    assert [m.part_number for m in hits] == ["BSC014N04LS", "BSC030N04LS"]  # sorted by Rds_on


def test_query_empty_on_impossible_constraints(lib: Library) -> None:
    assert query_mosfets(lib, Vds_min=1000.0) == []


def test_query_mosfets_by_package(lib: Library) -> None:
    hits = query_mosfets(lib, package="PG-TDSON-8")
    assert len(hits) >= 4
    assert all(m.package == "PG-TDSON-8" for m in hits)


# ---------- schema sanity validators fire ----------


def test_schema_rejects_miller_charge_out_of_band() -> None:
    """Qgd/Qg ratio outside [0.05, 0.7] = datasheet typo — must raise."""
    base = dict(
        part_number="X",
        Vds_max=40.0,
        Rds_on=0.005,
        Qg=49e-9,
        Qgd=49e-9,  # ratio 1.0 — impossible
        V_plateau=4.5,
        Ciss=3900e-12,
        Id_max=50.0,
        package="TDSON",
        die_x_mm=5.0,
        die_y_mm=6.0,
        die_z_mm=1.0,
        R_theta_jc=1.0,
        R_theta_ja=50.0,
        datasheet={"url": "https://example.com/x.pdf"},
    )
    with pytest.raises(Exception):
        MOSFET.model_validate(base)


def test_schema_rejects_inductor_isat_below_irms() -> None:
    with pytest.raises(Exception):
        Inductor.model_validate(
            dict(
                part_number="Y",
                L=1e-6,
                DCR=0.007,
                Isat=5.0,
                Irms=10.0,  # Irms > Isat — impossible
                f_self_res=50e6,
                package="4020",
                datasheet={"url": "https://example.com/y.pdf"},
            )
        )


# ---------- numeric package codes survive YAML round-trip ----------


def test_numeric_package_codes_stay_strings(lib: Library) -> None:
    caps = query_capacitors(lib, C_min=5e-6)
    assert all(isinstance(c.package, str) for c in caps)
    ind = lib.inductors["74437346010"]
    assert isinstance(ind.package, str) and ind.package == "7345"
