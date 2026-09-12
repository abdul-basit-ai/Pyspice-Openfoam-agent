"""Phase 11a: long-term design memory — spec class -> outcome records.

Git-versioned JSON (plan decision): the store is a single JSON file under
memory_store/, committed with the repo so past outcomes are diffable,
inspectable, and free. The agent reads it at run start (skip simulations a
similar spec already answered) and appends at run end.

Record schema:
  {
    "signature": "a1b2c3...",          # spec_signature hash
    "spec": {Vin, Vout, Iout, fsw_khz, ripple_ratio, Vripple},
    "attempts": [                      # one per run that touched this class
      {"date": ISO, "best_config": {...}, "tj_max_k": ..., "efficiency": ...,
       "converged": bool, "notes": [...]}
    ]
  }
Deduplication: same-signature runs APPEND to attempts (history is valuable);
lookup returns the most recent attempt first.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pyspice_openfoam_agent.orchestrator.tools import spec_signature


class DesignMemory:
    def __init__(self, run_dir: str | Path) -> None:
        # store lives at the REPO's memory_store/ (shared across runs), not
        # the per-run dir; run_dir only anchors the relative path search.
        here = Path(run_dir)
        # Prefer an EXISTING memory_store up the tree (repo-level store in
        # production); otherwise create one beside the run dir. The old
        # parent.parent fallback shared one store across unrelated tmp dirs —
        # cross-run pollution (audit finding).
        candidates = [
            here / "memory_store",
            here.parent / "memory_store",
            here.parent.parent / "memory_store",
        ]
        existing = next((c for c in candidates if c.is_dir()), None)
        if existing is not None:
            self.path = existing
        else:
            self.path = here / "memory_store"
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "design_memory.json"

    def _load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text())
            except json.JSONDecodeError:
                pass  # corrupted: start fresh, don't crash the agent
        return {"records": {}}

    def _save(self, store: dict) -> None:
        self.file.write_text(json.dumps(store, indent=2, sort_keys=True))

    def lookup(self, Vin: float, Vout: float, Iout: float, fsw_khz: float) -> list[dict]:
        sig = spec_signature(Vin, Vout, Iout, fsw_khz)
        store = self._load()
        rec = store["records"].get(sig)
        if not rec:
            return []
        return rec.get("attempts", [])

    def record_outcome(
        self,
        Vin: float, Vout: float, Iout: float, fsw_khz: float,
        spec_meta: dict, best_config: dict, outcome: dict,
    ) -> str:
        """Append one attempt. Returns the signature."""
        sig = spec_signature(Vin, Vout, Iout, fsw_khz)
        store = self._load()
        rec = store["records"].setdefault(sig, {"spec": spec_meta, "attempts": []})
        rec["spec"] = spec_meta  # refresh in case tolerance defaults changed
        rec["attempts"].append(
            {"date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "best_config": best_config, "outcome": outcome}
        )
        self._save(store)
        return sig
