"""Phase 8 standalone checkpoint: build + mesh + solve a real CHT case and
verify Tj extraction produces a physically-plausible result.

The plan's checkpoint: run against a known tutorial case and confirm
convergence within a bounded iteration count. Our test builds the real
Phase 6/7 case (12V→5V buck, 2.6W total loss) and verifies:
  1. solver converges (rc=0, no NaN)
  2. Tj_max > ambient (heat is being extracted)
  3. Tj_max < 150 degC (not wildly unphysical)
  4. per-device Tj is populated for all solid regions

Requires the docker-agent container (OpenFOAM + ngspice + Python).
"""

from __future__ import annotations

import pytest

from pyspice_openfoam_agent.library.loader import load_library
from pyspice_openfoam_agent.thermal.board import AMBIENT_TEMP_C, build_board_geometry
from pyspice_openfoam_agent.thermal.case_writer import build_case, run_mesh_pipeline
from pyspice_openfoam_agent.thermal.solver import SolverError, run_cht_solve


def _solver_available() -> bool:
    import shutil
    return shutil.which("chtMultiRegionSimpleFoam") is not None


_requires_solver = pytest.mark.skipif(
    not _solver_available(), reason="chtMultiRegionSimpleFoam not on PATH (run in-container)"
)


@pytest.fixture(scope="module")
def solved_case():
    """Build, mesh, and solve one real CHT case."""
    lib = load_library()
    geo = build_board_geometry(lib.mosfets["CSD16415Q5"])
    # Phase 5 loss values (verified earlier: ~2.6W total)
    power = {"hs_mosfet": 1.30, "ls_mosfet": 1.30, "inductor": 0.04}
    cp = build_case(geo, power, "runs/phase8_test")
    run_mesh_pipeline(cp.root, v_in_m_s=1.0)
    result = run_cht_solve(cp, end_time=100, timeout_s=600)
    return result


@_requires_solver
def test_checkpoint_solver_converges(solved_case):
    assert solved_case.converged
    assert solved_case.iterations > 0


@_requires_solver
def test_checkpoint_tj_above_ambient(solved_case):
    ambient_k = AMBIENT_TEMP_C + 273.15
    assert solved_case.tj_max is not None
    assert solved_case.tj_max > ambient_k, "Tj should rise above ambient with heat"


@_requires_solver
def test_checkpoint_tj_below_destruction(solved_case):
    # 150 degC = 423 K is the MOSFET Tj_max; a converged design must be below
    assert solved_case.tj_max < 423.0


@_requires_solver
def test_checkpoint_per_device_populated(solved_case):
    # all three heat sources must have a Tj reading
    for tag in ("hs_mosfet", "ls_mosfet", "inductor"):
        assert tag in solved_case.tj_per_device
        assert solved_case.tj_per_device[tag] > 0
