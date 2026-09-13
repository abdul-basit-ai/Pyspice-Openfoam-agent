"""Phase 15: run-state bridge between the orchestrator and the Streamlit UI.

The UI drives an orchestrated run and needs live progress without blocking
Streamlit's single-threaded model. This module owns a tiny JSON state file
per run (runs/<run_id>/state.json) that the orchestrator updates after every
tool call and the UI polls at ~2 Hz:

  {
    "run_id": "...", "status": "running|done|error",
    "step": 3, "max_steps": 12,
    "history": [{"tool": "size_converter", "ok": true, "summary": {...}}, ...],
    "artifacts": {...},            # ToolContext.artifacts mirror
    "final": {...} | null,
    "error": null | "..."
  }

Writing JSON per step is deliberately simple (no websocket plumbing): the
file is small, atomic-enough for one writer, and survives crashes — which
also gives us the Phase 11a crash-recovery story for free.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


class RunState:
    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "state.json"

    # ---------- writer (orchestrator side) ----------

    def init(self, task: dict, max_steps: int) -> None:
        self._write({
            "run_id": self.run_dir.name,
            "status": "running",
            "task": task,
            "step": 0,
            "max_steps": max_steps,
            "history": [],
            "artifacts": {},
            "final": None,
            "error": None,
            "updated": time.time(),
        })

    def log_tool(self, tool: str, ok: bool, summary: dict, artifacts: dict, step: int) -> None:
        state = self.read() or {}
        history = state.get("history", [])
        history.append({
            "tool": tool, "ok": ok, "summary": _compact(summary), "at": time.time(),
        })
        state["history"] = history
        state["artifacts"] = artifacts
        state["step"] = step
        state["updated"] = time.time()
        self._write(state)

    def finish(self, final: dict | None, error: str | None = None) -> None:
        state = self.read() or {}
        state["status"] = "error" if error else "done"
        state["final"] = final
        state["error"] = error
        state["updated"] = time.time()
        self._write(state)

    # ---------- reader (UI side) ----------

    def read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    # ---------- internals ----------

    def _write(self, state: dict) -> None:
        state["updated"] = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str))
        tmp.replace(self.path)  # atomic-enough on one writer


def _compact(summary: dict, limit: int = 40) -> dict:
    """Trim any large/awkward values out of a tool payload for the UI."""
    out = {}
    for k, v in summary.items():
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict):
            out[k] = {k2: (v2 if isinstance(v2, (int, float, str, bool)) else str(v2)[:60])
                      for k2, v2 in list(v.items())[:limit]}
        elif isinstance(v, list):
            out[k] = v[:limit]
        else:
            out[k] = str(v)[:100]
    return out


# ---------- orchestrator integration ----------


class StateLoggingHook:
    """Wire into the graph's act node: log every tool call to the run state."""

    def __init__(self, run_state: RunState) -> None:
        self.rs = run_state

    def on_tool(self, tool: str, ok: bool, summary: dict, artifacts: dict, step: int) -> None:
        self.rs.log_tool(tool, ok, summary, artifacts, step)
