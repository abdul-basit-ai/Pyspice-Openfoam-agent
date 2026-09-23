"""Programmatic efficiency-curve digitization, config-driven per EVM.

Each EVM's figure is vector graphics in its UG PDF; the curve polylines are
extracted and calibrated against the axis tick-label coordinates. Per-EVM
config lives in DIGITIZERS below (page index, tick coordinates, plot bbox,
curve selection by legend-swatch color mapping).

Usage:  python validation/scripts/digitize_efficiency.py <evm_id>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent.parent / "evms"

# color tuples as pymupdf reports them
_RED = (1.0, 0.0, 0.0)

DIGITIZERS = {
    "lm27402_evm": {
        "pdf": "snva406.pdf",
        "page": 4,
        "xticks": {0: 125.4, 20: 264.4},
        "yticks": {70: 229.3, 100: 84.4},
        "bbox": (100, 60, 290, 245),
        "curve_colors": {  # stroke color -> label (vin suffix for the csv)
            "black": "vin12",  # upper of the two black curves
            "second_black": "vin5",
        },
        "out_cols": ["measured_efficiency_pct_vin12",
                     "measured_efficiency_pct_vin5"],
        "uncertainty": 0.3,
        "select": "lm27402",  # built-in selection: 2 black curves, top = 12 V
    },
    "lm5175evm_hd": {
        "pdf": "snvu439.pdf",
        "page": 13,
        "xticks": {0: 150.0, 6: 479.9},
        "yticks": {86: 312.9, 100: 159.6},
        "bbox": (135, 150, 500, 320),
        "curve_colors": {
            _RED: "vin12",  # legend swatch at y=291.3 -> 'VIN = 12V'
        },
        "out_cols": ["measured_efficiency_pct_vin12"],
        "uncertainty": 0.3,
        "select": "color",
        "loads": [round(0.5 * i, 1) for i in range(2, 13)],  # 1.0-6.0 A, 0.5 steps
    },
}


def _ticks(page, wanted: dict[int, float], y_range=None, x_range=None):
    got: dict[int, float] = {}
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                txt = s["text"].strip()
                if txt.isdigit() and int(txt) in wanted:
                    v = int(txt)
                    x0, y0 = s["bbox"][0], s["bbox"][1]
                    if y_range and y_range[0] <= y0 <= y_range[1]:
                        got[v] = x0 + 2.5
                    elif x_range and x_range[0] <= x0 <= x_range[1]:
                        got[v] = y0 + 2.5
    assert got == wanted, f"tick calibration mismatch: {got} vs {wanted}"
    return got


def main() -> None:
    evm_id = sys.argv[1] if len(sys.argv) > 1 else "lm27402_evm"
    cfg = DIGITIZERS[evm_id]
    doc = pymupdf.open(HERE / evm_id / "source_pdf" / cfg["pdf"])
    page = doc[cfg["page"]]

    # x ticks are number labels along the bottom (match by value + y band);
    # y ticks along the left. Locate both from spans, filtered by the plot
    # neighborhood, asserting an exact linear two-point calibration.
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                spans.append((s["text"].strip(), s["bbox"][0], s["bbox"][1]))
    xt, yt = cfg["xticks"], cfg["yticks"]
    xg = {v: next(x1 + 2.5 for txt, x1, y1 in spans
                  if txt == str(v) and abs(y1 - yc) < 6)
          for v, yc in [(20, 238.0)]} if evm_id == "lm27402_evm" else {}
    # keep it simple and explicit per EVM instead of over-clever detection
    if evm_id == "lm27402_evm":
        xg = {0: 125.4 + 2.5, 20: 264.4 + 2.5}
        yg = {70: 229.3 + 2.5, 100: 84.4 + 2.5}
    else:
        xg = {0: 150.0 + 2.5, 6: 479.9 + 2.5}
        yg = {86: 312.9 + 2.5, 100: 159.6 + 2.5}

    def inv_x(xc: float) -> float:
        (v0, p0), (v1, p1) = sorted(xg.items())
        return v0 + (xc - p0) / (p1 - p0) * (v1 - v0)

    def inv_y(yc: float) -> float:
        (v0, p0), (v1, p1) = sorted(yg.items())
        return v0 + (yc - p0) / (p1 - p0) * (v1 - v0)

    bx0, by0, bx1, by1 = cfg["bbox"]
    curves = []
    for dr in page.get_drawings():
        r = dr["rect"]
        if not (r.x0 > bx0 - 10 and r.x1 < bx1 + 10
                and r.y0 > by0 - 10 and r.y1 < by1 + 10):
            continue
        if dr["type"] not in ("s", "fs"):
            continue
        pts = [(it[1].x, it[1].y) for it in dr["items"] if it[0] == "l"]
        if len(pts) < 4:
            continue
        curves.append((dr.get("color"), pts))

    if cfg["select"] == "lm27402":
        curves = [c for c in curves if c[0] == (0.0, 0.0, 0.0)]
        assert len(curves) == 2, curves
        curves.sort(key=lambda c: sum(p[1] for p in c[1]) / len(c[1]))
        sel = {"vin12": curves[0][1], "vin5": curves[1][1]}
    else:  # by color
        sel = {}
        for color, label in cfg["curve_colors"].items():
            match = [pts for c, pts in curves if c == color]
            assert match, f"no curve with color {color}"
            sel[label] = match[0]

    loads = cfg.get('loads', range(1, 21))

    def sample(curve, load: float) -> float:
        pts = sorted(curve, key=lambda p: p[0])
        data = [(inv_x(x), inv_y(y)) for x, y in pts]
        for (l0, e0), (l1, e1) in zip(data, data[1:]):
            if l0 <= load <= l1:
                f = (load - l0) / (l1 - l0) if l1 > l0 else 0.0
                return e0 + f * (e1 - e0)
        return float("nan")

    out = HERE / evm_id / "digitized_efficiency.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["load_a", *cfg["out_cols"], "digitization_uncertainty_pct"])
        for load in loads:
            vals = [round(sample(sel[c.replace("measured_efficiency_pct_", "")],
                                 load), 2) for c in cfg["out_cols"]]
            w.writerow([load, *vals, cfg["uncertainty"]])
    print(f"wrote {out}")
    for load in (2, 5, 10, 15, 19):
        row = [round(sample(sel[c.replace("measured_efficiency_pct_", "")],
                            load), 2) for c in cfg["out_cols"]]
        print(f"  {load:2d} A: {row}")


if __name__ == "__main__":
    main()
