import subprocess
import pytest
from PySpice.Spice.Netlist import Circuit
from PySpice.Unit import u_V, u_kOhm
from pyspice_openfoam_agent.config import settings


def test_pyspice_ngspice_binding():
    """Verify PySpice can drive the shared libngspice library."""
    circuit = Circuit("Sanity Check Divider")
    circuit.V("input", "in_node", circuit.gnd, 10 @ u_V)
    circuit.R(1, "in_node", "out_node", 1 @ u_kOhm)
    circuit.R(2, "out_node", circuit.gnd, 1 @ u_kOhm)

    simulator = circuit.simulator()
    analysis = simulator.operating_point()
    v_out = float(analysis.out_node)

    assert (
        abs(v_out - 5.0) < 1e-3
    ), f"Expected 5.0V at divider node, got {v_out}V"


def test_openfoam_binaries_present():
    """Verify that OpenFOAM v2406 environment variables and solvers are available."""
    res_block = subprocess.run(
        ["which", settings.BLOCK_MESH_BIN],
        capture_output=True,
        text=True,
    )
    assert (
        res_block.returncode == 0
    ), "blockMesh binary not found in system PATH"

    res_solver = subprocess.run(
        ["which", settings.SOLVER_BIN],
        capture_output=True,
        text=True,
    )
    assert (
        res_solver.returncode == 0
    ), f"{settings.SOLVER_BIN} binary not found in system PATH"


def test_package_paths():
    """Verify project paths configured in settings resolve properly."""
    assert settings.ROOT_DIR.exists(), "Root directory path does not resolve"