"""Phase 5: netlist connectivity validation (goals: floating nodes, invalid
connections, rating checks — before any simulation)."""

from __future__ import annotations

import re
from collections import Counter
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


def _physical_lines(text: str) -> list[str]:
    """Strip comments/blank lines, join '+' continuation lines, and drop the
    FIRST line (SPICE treats it as a title, never as a device — the old
    parser fed the title into the element grammar and survived only because
    its first letter happened not to look like an element, audit fix)."""
    raw: list[str] = []
    for line in text.splitlines()[1:]:
        stripped = line.split(";")[0].strip()  # trailing ';' comment
        if stripped and not stripped.startswith(("*", ".")):
            raw.append(stripped)
    joined: list[str] = []
    for line in raw:
        if line.startswith("+") and joined:
            joined[-1] = joined[-1].rstrip() + " " + line[1:].strip()
        else:
            joined.append(line)
    return joined


def validate_netlist(cir_path: str | Path) -> ConnectivityReport:
    """Static connectivity check on a Phase 3 netlist.

    A node is floating if it appears exactly once (connected to only one
    device pin) — no current path. Device pins are counted per element line.
    """
    device_names: list[str] = []
    node_degree: dict[str, int] = {}

    for line in _physical_lines(Path(cir_path).read_text(encoding="utf-8")):
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
            elif etype in ("s", "w", "m"):
                # switch / mosfet: 4 nodes (n+, n-, c+, c-); the model name
                # is a trailing token, not a node. Dropping the control node
                # here borks the connectivity graph (P5 bug: gate nodes
                # falsely flagged floating).
                nodes = tokens[1:5]
            elif etype == "x":  # subcircuit: node count unknown, skip pins
                continue
            else:
                nodes = tokens[1:3]
            device_names.append(name)
            for n in nodes:
                node_degree[n] = node_degree.get(n, 0) + 1

    # duplicates (raw element names — "Lout" vs "Cout" are distinct, so never
    # strip the leading element letter before comparing, as that would make
    # Lout/Cout collide on "out")
    dups = sorted(n for n, c in Counter(device_names).items() if c > 1)

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
