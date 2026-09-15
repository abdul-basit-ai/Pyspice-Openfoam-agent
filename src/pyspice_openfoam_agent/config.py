"""Global project settings and execution paths."""

from pathlib import Path
from pydantic import BaseModel, Field


class Settings(BaseModel):
    # Base paths relative to repository root
    ROOT_DIR: Path = Path(__file__).resolve().parent.parent.parent
    TEMPLATES_DIR: Path = ROOT_DIR / "templates"
    RUNS_DIR: Path = ROOT_DIR / "runs"
    MEMORY_STORE_DIR: Path = ROOT_DIR / "memory_store"
    LIBRARY_DATA_DIR: Path = (
        ROOT_DIR / "src" / "pyspice_openfoam_agent" / "library" / "data"
    )

    # OpenFOAM executable commands
    BLOCK_MESH_BIN: str = "blockMesh"
    SOLVER_BIN: str = "chtMultiRegionSimpleFoam"  # the multi-region SIMPLE solver actually invoked

    # Simulation defaults
    DEFAULT_AMBIENT_TEMP_K: float = 298.15  # 25 °C
    MAX_SIMULATION_CYCLES: int = 5000


settings = Settings()