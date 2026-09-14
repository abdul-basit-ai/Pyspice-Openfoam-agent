"""Phase 5: netlist connectivity validation (goals: floating nodes, invalid
connections, rating checks — before any simulation)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConnectivityReport:
    valid: bool
    floating_nodes: list[str]
    unconnected_devices: list[str]
    duplicate_device_names: list[str]
    node_degree: dict[str, int] = field(default_factory=dict)


# Node names considered intentional ground/reference (never "floating")
_GROUND_RE = re.compile(r"^(0|gnd|ground)$", re.I)


def validate_netlist(cir_path: str | Path) -> ConnectivityReport:
    """Static connectivity check on a Phase 3 netlist.

    A node is floating if it appears exactly once (connected to only one
    device pin) — no current path. Device pins are counted per element line.
    """
    text = Path(cir_path).read_text()
    device_names: list[str] = []
    node_degree: dict[str, int] = {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("*", ".", ";")):
            continue
        tokens = line.split()
        if not tokens:
            continue
        name = tokens[0]
        if name[0].lower() in "vircldsx" or name.lower().startswith("x") or name.lower().startswith("e"):
            if len(tokens) < 3:
                continue
            # element pin nodes: skip the element type prefix letter; for a
            # 2-terminal element: tokens[1], tokens[2]; source name is last
            # for V/I sources — approximate: treat tokens[1:-1] as nodes for
            # V/I, tokens[1:3] for R/L/C/D. This covers our Phase 3 netlists.
            etype = name[0].lower()
            if etype in ("v", "i"):
                nodes = tokens[1:3]
            elif etype == "x":  # subcircuit: node count unknown, skip pins
                continue
            else:
                nodes = tokens[1:3]
            device_names.append(name)
            for n in nodes:
                if n.startswith("gate_"):  # gate drive nodes are control, not power path
                    node_degree[n] = node_degree.get(n, 0) + 1
                    continue
                node_degree[n] = node_degree.get(n, 0) + 1

    # duplicates
    seen: dict[str, int] = {}
    dups = []
    for n in device_names:
        base = n.lstrip("vicrldsx VICRLDSX")
        seen[base] = seen.get(base, 0) + 1
    dups = [n for n, c in seen.items() if c > 1 and n]

    # floating: non-ground nodes with degree 1
    floating = sorted(
        n for n, deg in node_degree.items()
        if deg <= 1 and not _GROUND_RE.match(n)
    )

    valid = not floating and not dups
    return ConnectivityReport(
        valid=valid,
        floating_nodes=floating,
        unconnected_devices=[],
        duplicate_device_names=dups,
        node_degree=node_degree,
    )