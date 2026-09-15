"""Phase 11a: long-term design memory — spec class -> outcome records.

Git-versioned JSON (plan decision): the store is a single JSON file under
memory_store/, committed with the repo so past outcomes are diffable,
inspectable, and free. The agent reads it at run start (skip simulations a
similar spec already answered) and appends at run end.

Record schema:
  {
    "signature": "a1b2c3...",          # spec_signature hash
    "spec": {Vin, Vout, Iout, fsw_khz, ripple_ratio, Vripple},
    "attempts": [                      # one per run that touched this class,
                                       # in APPEND order (oldest first)
      {"date": ISO, "best_config": {...}, "outcome": {...}}
    ]
  }
Deduplication: same-signature runs APPEND to attempts (history is valuable);
lookup returns attempts oldest-first (the caller decides what "most recent"
means — the date field is on every attempt).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pyspice_openfoam_agent.orchestrator.tools import spec_signature

# How many parent levels to search for an existing memory_store/ before
# falling back to creating one beside the run dir (the old fixed 3-level
# search silently created a NEW store for run dirs nested deeper, e.g.
# runs/topology_smoke/<stamp>/<topology>/, splitting the memory).
_MAX_PARENT_SEARCH = 8


class DesignMemory:
    def __init__(self, run_dir: str | Path) -> None:
        # store lives at the REPO's memory_store/ (shared across runs), not
        # the per-run dir; run_dir only anchors the relative path search.
        # Prefer an EXISTING memory_store up the tree (repo-level store in
        # production); otherwise create one beside the run dir. The old
        # parent.parent fallback shared one store across unrelated tmp dirs —
        # cross-run pollution (audit finding).
        here = Path(run_dir).resolve()
        existing = None
        for cand in [here, *here.parents]:
            if (cand / "memory_store").is_dir():
                existing = cand / "memory_store"
                break
            if len(cand.parents) > _MAX_PARENT_SEARCH:
                break
        if existing is not None:
            self.path = existing
        else:
            self.path = here / "memory_store"
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = self.path / "design_memory.json"

    def _load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # Corrupted (e.g. a crash mid-write): QUARANTINE the file so
                # the evidence is inspectable, then start fresh. The old
                # behavior silently discarded the whole store on read
                # (audit finding) — repeated crashes could erase all history
                # with no trace.
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                quarantine = self.file.with_suffix(f".corrupt-{stamp}.json")
                try:
                    os.replace(self.file, quarantine)
                except OSError:
                    pass
        return {"records": {}}

    def _save(self, store: dict) -> None:
        # Atomic write (temp file + rename): a crash mid-write previously
        # left a truncated JSON that the next _load silently discarded
        # (audit finding).
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.file)

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
