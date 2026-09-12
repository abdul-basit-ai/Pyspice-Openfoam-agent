"""Phase 7: regenerate per-region field boundary conditions for OUR patches.

The v2406 cpuCabinet tutorial uses STL-specific patch names (CABINET,
FAN_SHROUD, FINS...). Our splitMeshRegions case has different patches
(inlet, outlet, topWall, bottomWall, sideWall_y0/y1 + mapped interfaces
like air_to_board / hs_mosfet_to_board). The solver reads literal patch
names, so we regenerate every 0.orig/<region>/<field> boundaryField.

BC recipe (per the tutorial's proven conditions, field-by-field):

  FLUID (air):
    patch      U                     T                     p/p_rgh          k,epsilon       nut,alphat
    inlet      fixedValue(v)         fixedValue(ambient)   zeroGradient     fixedValue      calculated
    outlet     zeroGradient          zeroGradient          fixedValue 0     zeroGradient    calculated
    wall       noSlip                externalWallHeatFlux  zeroGradient     lowRe/zeroGrad  calculated
                                      (q=0, adiabatic)
    interface  fixedValue (0 0 0)    compressible::turbulentTemperatureRadCoupledMixed
                                      + Tnbr T              zeroGradient     zeroGradient    calculated

  SOLID (board/devices) — T only:
    exterior (bottomWall, exposed sides): externalWallHeatFluxTemperature (q=0)
    interface:  compressible::turbulentTemperatureRadCoupledMixed; Tnbr T;
                neighbourFieldName T; kappaMethod fluidThermo; kappaName none

Patch roles come from the region's own polyMesh/boundary after split.
"""

from __future__ import annotations

from dataclasses import dataclass

from pathlib import Path  # noqa: I001  (used by _patch_list_of_region)
P_REFERENCE_PA = 101325.0  # 1 atm absolute, matches fields internalField




@dataclass(frozen=True)
class PatchRole:
    inlet: bool = False
    outlet: bool = False
    wall: bool = False
    interface: bool = False
    neighbour: str | None = None  # other region this patch couples to


def classify_patch(patch: str, region: str) -> PatchRole:
    """Classify one boundary patch of `region` into its role."""
    if patch == "inlet":
        return PatchRole(inlet=True)
    if patch == "outlet":
        return PatchRole(outlet=True)
    if patch in ("topWall", "bottomWall") or patch.startswith("sideWall"):
        return PatchRole(wall=True)
    # coupled interface: name is "<neighbour>_to_<region>" or "<region>_to_<neighbour>"
    if "_to_" in patch:
        a, b = patch.split("_to_", 1)
        neighbour = b if a == region else (a if b == region else patch)
        return PatchRole(interface=True, neighbour=neighbour)
    return PatchRole(wall=True)  # unknown → treat as wall (conservative)


# ---------- fluid field BC generators ----------
def fluid_bc(
    field: str,
    region: str,
    patches: list[str],
    v_in: tuple[float, float, float],
    ambient_k: float,
) -> str:
    """boundaryField body for a fluid field (no FoamFile header)."""
    entries: list[str] = []
    for p in patches:
        role = classify_patch(p, region)
        entries.append(_fluid_entry(field, p, role, v_in, ambient_k))
    return "\n".join(entries)


def _fluid_entry(field: str, patch: str, role: PatchRole, v_in, ambient_k: float) -> str:
    ftype = field.split("#")[0].split(":")[0]
    if field in ("U",):
        return _bc(patch, _uv_value(role, v_in))
    if field == "p":
        # absolute pressure: outlet pins the reference (internalField is also
        # 101325, so the outlet must match, not be 0 — an outlet-at-0 while
        # internal=1 atm is a step that NaNs the first solve, audit finding);
        # inflow/interface see zeroGradient.
        if role.outlet:
            return _bc(patch, f"type fixedValue;\n        value uniform {P_REFERENCE_PA:f};")
        return _bc(patch, "type zeroGradient;")
    if field == "p_rgh":
        # Buoyant compressible p_rgh: walls/inlet/interface use fixedFluxPressure
        # (matches internal reference, couples to velocity). The OUTLET must
        # use fixedValue $internalField (NOT fixedFluxPressure — that makes the
        # pressure equation singular and NaNs the first momentum solve; audit
        # finding confirmed against the v2406 cpuCabinet tutorial which uses
        # fixedValue at outlets).
        if role.outlet:
            return _bc(patch, "type fixedValue;\n        value $internalField;")
        return _bc(patch, "type fixedFluxPressure;\n        value $internalField;")
    if field in ("k", "epsilon"):
        if role.inlet:
            # nominal turbulence intensity 5%, k ~ 1.5*(I*U)^2 with U~1 m/s
            u_mag = (v_in[0] ** 2 + v_in[1] ** 2 + v_in[2] ** 2) ** 0.5
            k_in = 1.5 * (0.05 * u_mag) ** 2
            target = {"k": f"uniform {k_in:.6e}", "epsilon": f"uniform {1e-6:.6e}"}[field]
            return _bc(patch, f"type fixedValue;\n        value {target};")
        return _bc(patch, "type zeroGradient;")
    if field in ("nut", "alphat"):
        return _bc(patch, "type calculated;\n        value uniform 0;")
    if field == "T":
        if role.inlet:
            return _bc(patch, f"type fixedValue;\n        value uniform {ambient_k:.3f};")
        if role.outlet:
            # convective outflow: temperature advects out
            return _bc(patch, "type zeroGradient;")
        if role.interface:
            # fluid side of conjugated interface
            return _bc(patch, _fluid_t_interface())
        # walls: adiabatic (q=0) external-wall heat-flux
        return _bc(patch, _ext_wall_heated(kappa="fluidThermo"))
    return _bc(patch, "type zeroGradient;")


def _uv_value(role: PatchRole, v_in) -> str:
    if role.inlet:
        u = " ".join(str(round(float(x), 6)) for x in v_in)
        return f"type fixedValue;\n        value uniform ({u});"
    if role.interface:
        return "type fixedValue;\n        value uniform (0 0 0);"
    return "type noSlip;" if not role.outlet else "type zeroGradient;"


def _fluid_t_interface() -> str:
    return (
        "type            compressible::turbulentTemperatureRadCoupledMixed;\n"
        "Tnbr            T;\n"
        "neighbourFieldName T;\n"
        "kappaMethod     fluidThermo;\n"
        "kappaName       none;\n"
        "value           $internalField;"
    )


def _ext_wall_heated(kappa: str) -> str:
    return (
        "type            externalWallHeatFluxTemperature;\n"
        "mode            flux;\n"
        f"kappaMethod     {kappa};\n"
        "kappaName       none;\n"
        "Ta              $internalField;\n"
        "q               uniform 0;\n"
        "value           $internalField;"
    )


def _bc(patch: str, body: str) -> str:
    return f"    {patch}\n    {{\n        {body}\n    }}"


# ---------- solid field BC generators ----------


def solid_bc(field: str, region: str, patches: list[str], ambient_k: float) -> str:
    entries: list[str] = []
    for p in patches:
        role = classify_patch(p, region)
        if field == "T":
            if role.interface:
                entries.append(_bc(p, _solid_t_interface()))
            else:
                entries.append(_bc(p, _ext_wall_heated(kappa="solidThermo")))
        elif field == "p":
            entries.append(_bc(p, "type zeroGradient;"))
        else:
            entries.append(_bc(p, "type zeroGradient;"))
    return "\n".join(entries)


def _solid_t_interface() -> str:
    return (
        "type            compressible::turbulentTemperatureRadCoupledMixed;\n"
        "Tnbr            T;\n"
        "neighbourFieldName T;\n"
        "kappaMethod     solidThermo;\n"
        "kappaName       none;\n"
        "value           $internalField;"
    )


# ---------- field-file assembler ----------


def _patch_list_of_region(case_root, region) -> list[str]:
    """Read the region's boundary patch names from polyMesh/boundary."""
    import re

    b = Path(case_root) / "constant" / region / "polyMesh" / "boundary"
    if not b.exists():
        return []
    text = b.read_text(encoding="utf-8", errors="replace")
    # patch names are top-level dict keys in OpenFOAM boundary file
    names: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(("//", "/*", "*/", "{", "}", ")")) and not s.startswith((
            "FoamFile", "version", "format", "class", "object", "note", "location",
            "dimensionedTypes", "defaultPatch", "(",
        )):
            # a name is a bare word at the start of a line inside the file
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s) and s not in ("boundary",):
                names.append(s)
    # dedupe preserving order
    seen = set()
    out = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def rewrite_field_bcs(
    case_root: Path,
    regions: list[str],
    v_in: tuple[float, float, float],
    ambient_k: float,
) -> None:
    """Rewrite the boundaryField of every 0.orig/<region>/<field> in place."""
    zero = case_root / "0.orig"
    for region in regions:
        patches = _patch_list_of_region(case_root, region)
        rd = zero / region
        if not rd.is_dir() or not patches:
            continue
        for field_file in rd.iterdir():
            if not field_file.is_file():
                continue
            field = field_file.name
            if region == "air":
                bcs = fluid_bc(field, region, patches, v_in, ambient_k)
            else:
                bcs = solid_bc(field, region, patches, ambient_k)
            _rewrite_field_keep_rest(field_file, bcs)


def _rewrite_field_keep_rest(path: Path, new_boundary_body: str) -> None:
    """Replace only the boundaryField block of a field file, preserving
    FoamFile header, dimensions, internalField, and the field body."""
    text = path.read_text(encoding="utf-8", errors="replace")
    marker = "boundaryField"
    idx = text.find(marker)
    if idx < 0:
        raise ValueError(f"no boundaryField in {path}")
    # find the opening brace after the marker
    lb = text.find("{", idx)
    if lb < 0:
        raise ValueError(f"no brace after boundaryField in {path}")
    # find matching close brace
    depth = 1
    rb = lb
    i = lb + 1
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        rb = i
        i += 1
    head = text[: idx + len(marker)]
    tail = text[rb + 1:]
    new = head + "\n{\n" + new_boundary_body + "\n}\n" + tail
    path.write_text(new, encoding="utf-8")
