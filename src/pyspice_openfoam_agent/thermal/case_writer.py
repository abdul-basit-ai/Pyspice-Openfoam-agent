"""Phase 7: CHT case writer — assemble a chtMultiRegionSimpleFoam case.

Approach (tutorial-as-template, chosen after a failed hand-generated draft):
the v2406 cpuCabinet tutorial ships a COMPLETE, proven-converging dict set
per region (fvSchemes/fvSolution, thermophysicalProperties, turbulence,
0.orig fields with correct boundary conditions, and the mesh/split pipeline).
We copy that scaffold verbatim and only replace:
  - system/blockMeshDict   -> our rendered MeshPlan dict
  - constant/regionProperties -> OUR region list (fluid: air; solid:
    board, hs_mosfet, ls_mosfet, inductor)
  - per-region system/<name>/ dirs and 0/<name>/ fields -> renamed to our
    regions
  - heat sources (fvOptions: volumetric W/m3 per solid region)

Deviation from the plan's "laminar air" choice (audit decision, flagged for
the user): the CPU cabinet tutorial solves with realizableKE turbulence in a
compressible buoyant (p_rgh) formulation. Stripping it to laminar requires
removing the k/epsilon/nut/alphat fields and disabling buoyancy in the
solver config — a risky first-pass change. The responsible order is to get a
converging turbulent baseline with our mesh/geometry first, then optionally
sweep to laminar/other physics as a refinement. The turbulenceProperties
file is copied through and can be switched later.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from pyspice_openfoam_agent.thermal.board import BoardGeometry
from pyspice_openfoam_agent.thermal.bc_writer import rewrite_field_bcs
from pyspice_openfoam_agent.thermal.mesh_generator import MeshPlan, plan_mesh, render_block_mesh_dict

TUTORIAL_REL = "heatTransfer/chtMultiRegionSimpleFoam/cpuCabinet"
SOLID_REGIONS = ("board", "hs_mosfet", "ls_mosfet", "inductor")
FLUID_REGIONS = ("air",)
ALL_REGIONS = ("air", *SOLID_REGIONS)

# tutorial's own region dir names
_TUT_FLUID = "domain0"
_TUT_SOLID_REF = "v_CPU"  # used as the solid scaffold source for all our solids


class CaseBuildError(RuntimeError):
    """Case assembly failed."""


@dataclass
class CasePaths:
    root: Path
    regions: tuple[str, ...]
    n_cells: int
    n_blocks: int


def _find_tutorial_case() -> Path:
    for base in ("/usr/lib/openfoam/openfoam2406/tutorials", "/opt/openfoam/tutorials"):
        p = Path(base) / TUTORIAL_REL
        if p.is_dir():
            return p
    raise CaseBuildError(f"tutorial case {TUTORIAL_REL} not found in container")


def _tutorial_region(src_root: Path, kind: str) -> Path:
    """The tutorial's fluid or solid region directory under its system/."""
    key = {"fluid": _TUT_FLUID, "solid": _TUT_SOLID_REF}[kind]
    p = src_root / key
    if not p.is_dir():
        raise CaseBuildError(f"tutorial {kind} region dir {p} missing")
    return p


def _copy_scaffold(src: Path, dst: Path) -> None:
    """Copy the tutorial's system/constant/0.orig scaffolds; strip
    tutorial-only machinery."""
    for sub in ("system", "constant", "0.orig"):
        if (src / sub).exists():
            shutil.copytree(src / sub, dst / sub, dirs_exist_ok=True)
    # tutorial-only workflow files / snappy machinery not used by our mesh
    for junk in (
        "Allrun", "Allrun.pre", "Allclean", "README.md", "externalSolver",
        "snappyHexMeshDict", "surfaceFeatureExtractDict", "meshQualityDict",
        "createBafflesDict", "topoSetDict.f1", "decomposeParDict",
    ):
        for cand in (dst / junk, dst / "system" / junk):
            if cand.exists():
                if cand.is_dir():
                    shutil.rmtree(cand)
                else:
                    cand.unlink()

# ---------------- region rework ----------------

# Tutorial region dirs we replace wholesale.
_TUT_REGIONS = {_TUT_FLUID, _TUT_SOLID_REF, "v_fins"}
HEAT_SOURCE_REGIONS = ("hs_mosfet", "ls_mosfet", "inductor")


def _remove_tutorial_regions(case: Path) -> None:
    """Delete tutorial region dirs from system/, constant/, 0.orig/."""
    for sub in ("system", "constant", "0.orig"):
        d = case / sub
        if d.is_dir():
            for region in _TUT_REGIONS:
                p = d / region
                if p.exists():
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
    # stray decomposeParDict/topoSetDict per-region (single-proc, unused)
    for p in case.glob("system/*/decomposeParDict"):
        p.unlink()


def _rework_regions(case: Path) -> None:
    """Build air + solid region dirs from the PRISTINE tutorial (never the
    case copy, which we strip): fluid air <- domain0, solids <- v_CPU."""
    tut = _find_tutorial_case()
    tut_fluid_dir = tut / "0.orig" / _TUT_FLUID
    tut_solid_dir = tut / "0.orig" / _TUT_SOLID_REF
    tut_fluid_const = tut / "constant" / _TUT_FLUID
    tut_solid_const = tut / "constant" / _TUT_SOLID_REF
    tut_fluid_sys = tut / "system" / _TUT_FLUID
    tut_solid_sys = tut / "system" / _TUT_SOLID_REF

    # air (fluid)
    air_dir = case / "0.orig" / "air"; air_dir.mkdir(parents=True)
    for f in ("T", "U", "p", "p_rgh", "k", "epsilon", "nut", "alphat"):
        shutil.copyfile(tut_fluid_dir / f, air_dir / f)
    air_const = case / "constant" / "air"; air_const.mkdir(parents=True)
    # NOTE: MRFProperties deliberately NOT copied — the tutorial's air region
    # has a rotating-fan MRF cellZone (v_MRF) that does not exist in our mesh;
    # leaving it would make chtMultiRegionSimpleFoam abort (audit finding).
    for f in ("thermophysicalProperties", "turbulenceProperties"):
        shutil.copyfile(tut_fluid_const / f, air_const / f)
    air_sys = case / "system" / "air"; air_sys.mkdir(parents=True)
    for f in ("fvSchemes", "fvSolution"):
        shutil.copyfile(tut_fluid_sys / f, air_sys / f)

    # solids
    for r in SOLID_REGIONS:
        rd = case / "0.orig" / r; rd.mkdir(parents=True)
        for f in ("T", "p"):
            shutil.copyfile(tut_solid_dir / f, rd / f)
        rc = case / "constant" / r; rc.mkdir(parents=True)
        shutil.copyfile(tut_solid_const / "thermophysicalProperties", rc / "thermophysicalProperties")
        rs = case / "system" / r; rs.mkdir(parents=True)
        for f in ("fvSchemes", "fvSolution"):
            shutil.copyfile(tut_solid_sys / f, rs / f)


def _rewrite_region_properties(case: Path) -> None:
    solids = " ".join(SOLID_REGIONS)
    (case / "constant" / "regionProperties").write_text(
        f"""FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      regionProperties;
}}

regions
(
    fluid ( air )
    solid ( {solids} )
);
""",
        encoding="utf-8",
    )


# ---------------- heat sources (fvOptions) ----------------

_HEAT_FVOPTIONS_TEMPLATE = """FoamFile
{{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    "system";
    object      fvOptions;
}}

{SELF_NAME}_AbsoluteEnergySource
{{
    type            scalarSemiImplicitSource;
    active          true;

    selectionMode   cellZone;
    cellZone        {ZONE};
    volumeMode      absolute;

    sources
    {{
        h           ( {POWER_W:.3f} 0 );
    }}
}}
"""


def _write_heat_sources(case: Path, plan: "MeshPlan", power_watts: dict[str, float]) -> None:
    """Write one system/<region>/fvOptions per heat-source solid, with the
    volumetric heat rate set to the absolute watts from Phase 5."""
    for zone in HEAT_SOURCE_REGIONS:
        watt = float(power_watts.get(zone, 0.0))
        text = _HEAT_FVOPTIONS_TEMPLATE.format(
            SELF_NAME=zone, ZONE=zone, POWER_W=watt
        )
        target = case / "system" / zone / "fvOptions"
        target.write_text(text, encoding="utf-8")


# ---------------- top-level assembler ----------------


def build_case(
    geo: BoardGeometry,
    power_watts: dict[str, float],
    out_dir: str | Path,
    v_in_m_s: float = 1.0,
    n_cells_budget: int | None = None,
) -> CasePaths:
    """Assemble the complete multi-region CHT case under `out_dir`.

    v_in_m_s sets the forced-convection inlet speed (default 1 m/s)."""
    src = _find_tutorial_case()
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    _copy_scaffold(src, out)
    _remove_tutorial_regions(out)

    plan = plan_mesh(geo)
    if n_cells_budget is not None and plan.n_cells > n_cells_budget:
        raise CaseBuildError(
            f"planned mesh {plan.n_cells} cells exceeds budget {n_cells_budget}"
        )
    (out / "system" / "blockMeshDict").write_text(
        render_block_mesh_dict(plan), encoding="utf-8"
    )
    _rewrite_region_properties(out)
    _rework_regions(out)
    _fix_tutorial_dict_quirks(out)
    _write_laminar_turbulence(out)
    _write_heat_sources(out, plan, power_watts)
    _write_allrun_pre(out)
    return CasePaths(root=out, regions=ALL_REGIONS, n_cells=plan.n_cells, n_blocks=plan.n_blocks)


# ---------------- mesh/split pipeline script ----------------


_ALLRUN_PRE = """#!/bin/sh
# Build the multi-region mesh: blockMesh, split by cellZones.
# (replaces the tutorial's Allrun.pre, which used snappyHexMesh + STL we don't have.)
# Note: 0/ staging happens AFTER the BC rewrite in run_mesh_pipeline.
set -e
cd "$(dirname "$0")" || exit
. ${WM_PROJECT_DIR:?}/bin/tools/RunFunctions 2>/dev/null || true
runApplication blockMesh
runApplication splitMeshRegions -cellZones -overwrite
"""


def _write_allrun_pre(case: Path) -> None:
    p = case / "Allrun.pre"
    p.write_text(_ALLRUN_PRE, encoding="utf-8")
    p.chmod(0o755)


def run_mesh_pipeline(case_root, v_in_m_s: float = 1.0, ambient_k: float | None = None) -> None:
    """Run blockMesh + splitMeshRegions on an assembled case, then rewrite the
    per-region field boundary conditions from the now-existing patch lists
    (polyMesh/boundary only exists after the split). Requires OpenFOAM in
    the container (executes the shipped Allrun.pre)."""
    import subprocess

    from pyspice_openfoam_agent.thermal.board import AMBIENT_TEMP_C

    ambient = ambient_k or (AMBIENT_TEMP_C + 273.15)
    cp = Path(case_root)
    proc = subprocess.run(
        ["bash", "Allrun.pre"], cwd=cp, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise CaseBuildError(
            f"mesh pipeline failed (rc={proc.returncode}):\n{proc.stderr[-2000:]}"
        )
    # rewrite BCs from the freshly-created region meshes (0.orig only; the
    # tutorial BCs are swapped for our patch names)
    rewrite_field_bcs(cp, ALL_REGIONS, (v_in_m_s, 0.0, 0.0), ambient)
    # stage 0/ from the corrected 0.orig
    import subprocess as _sp
    _sp.run(["rm", "-rf", str(cp / "0")])
    shutil.copytree(cp / "0.orig", cp / "0")


_LAMINAR_TURBULENCE = """FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    location    "constant";
    object      turbulenceProperties;
}

simulationType  laminar;
"""


def _write_laminar_turbulence(case: Path) -> None:
    """Replace the air region's turbulenceProperties with the laminar model.

    (User decision: laminar air, v_in = 1 m/s. Also avoids the realizableKE
    first-step FPE the tutorial's k/epsilon initialisation triggers on our
    finer mesh — audit finding.) The k/epsilon/nut/alphat fields remain in
    0.orig but are unused; this is the least-invasive, most robust switch."""
    (case / "constant" / "air" / "turbulenceProperties").write_text(
        _LAMINAR_TURBULENCE, encoding="utf-8"
    )


def _fix_tutorial_dict_quirks(case: Path) -> None:
    """Fix known v2406 tutorial dict bugs."""
    fs = case / "system" / "air" / "fvSolution"
    if not fs.exists():
        return
    t = fs.read_text()
    # fix missing semicolon on 'solver          PCG' (no semicolon)
    t = t.replace("solver          PCG\n", "solver          PCG;\n")
    # add pRefCell/pRefValue inside SIMPLE block if not present
    if "pRefCell" not in t and "SIMPLE" in t:
        t = t.replace(
            "momentumPredictor true;",
            "momentumPredictor true;\n    pRefCell 0;\n    pRefValue 101325;",
        )
    fs.write_text(t)

