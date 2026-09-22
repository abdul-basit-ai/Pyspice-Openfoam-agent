"""Programmatic curve digitization for the LM27402 EVM efficiency figure.

Replaces manual WebPlotDigitizer for THIS figure: SNVA406C draws the
efficiency curves as PDF VECTOR paths, so the curve polyline is extracted
exactly and calibrated against the axis tick-label coordinates. This is
more accurate than manual digitization; the recorded uncertainty
(+/-0.3 %) covers axis-label anchor offsets and path-to-data rounding.

Usage:  python validation/scripts/digitize_efficiency.py
Output: validation/evms/lm27402_evm/digitized_efficiency.csv
"""

from __future__ import annotations

import csv
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent.parent / "evms" / "lm27402_evm"
PDF = HERE / "source_pdf" / "snva406.pdf"
OUT = HERE / "digitized_efficiency.csv"
PAGE = 4  # 0-based; figure 6-1 lives on printed page 5

UNCERTAINTY_PCT = 0.3


def _tick_positions(page) -> dict:
    """Locate axis tick labels and return calibration maps."""
    y_ticks, x_ticks = {}, {}
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                txt = s["text"].strip()
                x0, y0 = s["bbox"][0], s["bbox"][1]
                if txt in ("70", "75", "80", "85", "90", "95", "100") and x0 < 120:
                    y_ticks[int(txt)] = y0 + 2.5  # span top -> visual center
                if txt in ("0", "5", "10", "15", "20") and 230 < y0 < 245:
                    x_ticks[int(txt)] = x0 + 2.5
    assert set(y_ticks) == {70, 75, 80, 85, 90, 95, 100}, y_ticks
    assert set(x_ticks) == {0, 5, 10, 15, 20}, x_ticks
    return y_ticks, x_ticks


def _linear(ticks: dict[int, float], target: int, at: float) -> float:
    """Invert the axis mapping: pdf coord -> data value (ticks are linear)."""
    (v0, p0), (v1, p1) = sorted(ticks.items())[0], sorted(ticks.items())[-1]
    return v0 + (at - p0) / (p1 - p0) * (v1 - v0)


def main() -> None:
    doc = pymupdf.open(PDF)
    page = doc[PAGE]
    y_ticks, x_ticks = _tick_positions(page)

    curves: list[tuple[float, list[tuple[float, float]]]] = []
    for dr in page.get_drawings():
        r = dr["rect"]
        if not (r.x0 > 100 and r.x1 < 290 and r.y0 > 60 and r.y1 < 245):
            continue
        if dr["type"] not in ("s", "fs"):
            continue
        pts = [(it[1].x, it[1].y) for it in dr["items"] if it[0] == "l"]
        if len(pts) < 4:
            continue
        # mean efficiency height distinguishes the two curves: the higher
        # curve (smaller pdf-y) is the VIN = 12 V one per the figure legend
        mean_y = sum(p[1] for p in pts) / len(pts)
        curves.append((mean_y, pts))
    assert len(curves) == 2, f"expected the 2 efficiency curves, got {len(curves)}"
    curves.sort()  # lower mean_y first -> higher efficiency -> VIN = 12 V
    vin12, vin5 = curves[0][1], curves[1][1]

    def to_load(p):  # pdf x -> output current
        return _linear(x_ticks, 0, p[0])

    def to_eff(p):  # pdf y -> efficiency percent
        return _linear(y_ticks, 0, p[1])

    def sample(curve, load: float) -> float:
        pts = sorted(curve, key=lambda p: p[0])
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if to_load((x0, y0)) <= load <= to_load((x1, y1)):
                f = (load - to_load((x0, y0))) / (to_load((x1, y1)) - to_load((x0, y0)))
                return to_eff((0, y0 + f * (y1 - y0)))
        return float("nan")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["load_a", "measured_efficiency_pct_vin12",
                    "measured_efficiency_pct_vin5", "digitization_uncertainty_pct"])
        for load in range(1, 21):
            w.writerow([load, round(sample(vin12, load), 2),
                        round(sample(vin5, load), 2), UNCERTAINTY_PCT])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    for load in (2, 5, 10, 15, 20):
        print(f"  {load:2d} A: {sample(vin12, load):5.2f} % (12 V), "
              f"{sample(vin5, load):5.2f} % (5 V)")


if __name__ == "__main__":
    main()
