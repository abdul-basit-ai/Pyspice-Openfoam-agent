# remain_plan.md — Phase Completion Status & Remaining Work

Created: 2026-09-17 (after commits through `0af71c1`, post-AUDIT.md).
**Updated 2026-09-19: Group A implemented** (see DONE log). Host suite now
**179 passed / 6 env-gated skips** (was 151/7); `topology_smoke.py` ALL PASSED
(buck / boost / buck_boost, host tier). In-container re-verification
(dtest.sh + smoke `--thermal`) still pending — needs disk headroom in `runs/`.

How to use: work top-to-bottom by priority group. Each item lists the gap, the
file(s) to touch, concrete steps, and an acceptance check. Tick items off as
you go. Re-run the verification block at the bottom after every group.

---

## Status summary (vs `updated_project_goals.md` Phases 0–23)

| Phase | Area | Status | What's missing (one line) |
|---|---|---|---|
| 0 | Environment & reproducibility | **~DONE** | LLM sampling settings not sent/logged (see 18); host venv was missing pymoo (now installed — keep pyproject install fresh) |
| 1 | Component database | **PARTIAL → A1 data done** | gate drivers (3) / controllers (4) / diodes (5) shipped; Coss/Crss still unverified per part (see A1 notes); core-loss fields remain descope-by-documentation |
| 2 | Spec parser & feasibility | **PARTIAL** | "36-60 V" ranges, MHz/Hz, V-unit ripple, efficiency phrasings still unsupported |
| 3 | Topology + sizing | **DONE** | (multi-criteria scorer is advisory-only — acceptable, documented) |
| 4 | Unified Design object | **PARTIAL** | Design still throwaway inside tools; not the persisted source of truth |
| 5 | Selection + schematic | **DONE (A2)** | gate-driver + controller IC selection wired into select_components with honest gap notes |
| 6 | Control loop | **DONE (A3, open-loop scope)** | step tests shipped (`run_step_tests`); closed-loop SPICE step testing documented as follow-on |
| 7 | Fast screening | **DONE** | (ripple prediction + banking + safety screens all in) |
| 8 | SPICE + steady state | **PARTIAL → A4 load+vin sweeps done** | temperature/tolerance sweeps remain descoped (documented in the tool) |
| 9 | Loss extraction | **DONE (A6)** | cap-ESR loss computed + reported; Vf/core-loss recompute stays data-blocked |
| 10 | Board geometry | **DONE** | (placement-as-optimization-variable = advanced, descoped) |
| 11 | Mesh + case generation | **~DONE** | Fidelity presets exist but not exposed through the `run_thermal` tool (Group C1) |
| 12 | CHT solve | **DONE** | (steady-state only, by documented scope decision) |
| 13 | Extraction + validation | **DONE** | — |
| 14 | Multi-objective optimization | **DONE (A5)** | `optimize_pareto` tool + CHT finalist verification; reduced-tier bugs fixed |
| 15 | Parallel exploration | **MISSING** | Serial NSGA-II only; stub was removed |
| 16 | Feedback / failure diagnosis | **DONE** | (auto-mitigation + caveats; HITL ask-before-change descoped) |
| 17 | Interactive UI | **PARTIAL (+ step/sweep/pareto panels)** | component edit / probe / compare still missing (Group C3) |
| 18 | AI orchestrator | **PARTIAL** | No per-call provenance record (temperature/prompt/response log) |
| 19 | Memory / knowledge base | **DONE** | — |
| 20 | HITL approval gates | **MISSING (by decision)** | Stubs removed; either implement minimal gates or record the descope in goals doc |
| 21 | Hardware validation | **NOT STARTED** | Requires physical bench — long-term item |
| 22 | Unified output package | **PARTIAL** | `bundle/manifest.py` exists but is never called by `graph.finalize` |
| 23 | Regression suite | **PARTIAL (+ Group A tests)** | Named cases still missing: thermal failure, infeasible spec, component substitution |

Doc fixes needed regardless: `CLAUDE.md` claims gate-driver/controller/diode
libraries + Jinja2 blockMeshDict templates + temperature logging — **none of
these exist**. Fix the doc (or make the code match it).

---

## Group A — Engineering capability gaps (highest value) — **IMPLEMENTED 2026-09-19**

> What shipped (all verified on host: 179 passed / 6 CHT-gated skips, smoke
> ALL PASSED; new tests in `tests/unit/test_group_a.py`):
>
> - **A1 data:** `library/data/gate_drivers.yaml` (UCC27517DBV, TC4420EOA,
>   LM5107MAX), `controllers.yaml` (TPS40057, LM27402, SG3525A, TL494 — all
>   voltage-mode), `diodes.yaml` (SS34/SS54/B340A/PMEG6030EP/MBR20100CT) —
>   every value web-verified against the linked datasheet; `query_gate_drivers
>   / query_controllers / query_diodes` in the loader.
> - **A2 selection:** `select_gate_driver` / `select_controller` wired into
>   `select_components` (buck → half-bridge LM5107MAX with an honest 8–14 V
>   aux-rail note; boost/bb → fastest adequate low-side driver; controller
>   filtered to voltage-mode + fsw window, gaps reported as notes, never
>   silent). Surfaces in `artifacts["components"]` + tool payload.
> - **A3 step tests:** new `spice/step_tests.py` (PWL step injection into a
>   copy of the netlist — builders untouched) + `run_step_tests` tool. Measures
>   pre/post levels, under/overshoot, settling to ±2%. Scope documented: the
>   rig is OPEN-LOOP (plant response); the compensated loop is validated via
>   the margin analysis. UI Results tab renders the table.
> - **A4 sweeps:** new `spice/sweeps.py` + `run_sweeps` tool. Load sweep =
>   Rload surgery on the servo'd netlist (fixed design; waveform efficiency
>   labeled as an upper bound since ideal switches carry no modeled switching
>   loss). Vin sweep = child pipeline per corner with per-point duty servo.
>   Temp/tolerance sweeps descoped in the tool docstring.
> - **A5 optimizer:** `optimize_pareto` tool (bounded NSGA-II) + two-tier
>   finalist verification re-entering the real pipeline (build → servo'd
>   SPICE → CHT) per finalist; UI renders finalists. Fixed three latent bugs
>   it exposed: `reduced_order_losses` divided by zero gate current for
>   V_plateau=5.0 parts (now uses the shared clamped `crossover_time`, which
>   also gained the guard), the Pareto collection phase resurrected
>   screened-out rows with buck-default physics, and both finalist/mitigation
>   child pipelines called run_spice without build_netlist first.
> - **A6 cap ESR loss:** `extract_losses(..., cap_esr=, iout=)` computes
>   ESR·I_cap_rms² from the recovered waveforms (buck: i_L−i_out; boost/bb:
>   pulsed sync-phase current), in `DeviceLosses.capacitor_esr` + total +
>   notes; both tool callers pass the selected capacitor.
>
> Leftovers from A1 (small, listed for the next pass):
> - Coss/Crss for the 12 MOSFETs still unset — populate only with
>   datasheet-verified numbers. NOTE: "SISS44DN10" could not be verified as a
>   real Vishay part (closest real part: SiSS4410DN, 40 V, Coss 168 pF /
>   Crss 20 pF) and its datasheet URL 404s — check whether the entry is a
>   mis-transcribed part number and fix the whole record if so.
> - Inductor core-loss Steinmetz fields stay None (documented descope);
>   DCR/ESR tempco remain schema defaults.

### A1. Phase 1 data: gate drivers, controllers, diodes (blocks A2/P5) — DONE (see above)
### A2. Phase 5: IC selection in the pipeline — DONE
### A3. Phase 6: load-step / input-step transient validation — DONE (open-loop scope; closed-loop SPICE step testing = follow-on)
### A4. Phase 8: operating-condition sweeps — DONE (load + vin; temp/tolerance descoped)
### A5. Phase 14: expose the optimizer + finalist CHT verification — DONE (needs the in-container run to exercise finalist CHT)
### A6. Phase 9: capacitor ESR loss — DONE

---

## Group B — Contract / policy gaps

### B1. Phase 2 parser: units & ranges (never silently change the spec)
- File: `sizing/spec_parser.py`.
- Steps:
  1. `_TO_PAIR`: also accept "36-60 V" / "36–60V" (V after second number
     only). On a matched input RANGE: keep worst-case single Vin for the
     engine, but add BOTH `vin_min`/`vin_max` to `Requirements` (optional
     fields, default None) + an assumption note "designed at worst-case
     endpoint". (Full range modeling = later item, C2.)
  2. `_FSW`: accept MHz/Hz ("2 MHz" → 2000 kHz; "500000 Hz").
  3. Ripple: accept V units ("ripple < 0.2 V") alongside mV.
  4. `_EFF`: widen the gap window (e.g. `{0,24}`) and add the
     number-before-keyword form ("95% efficiency").
  5. Sanity checks: UVLO > Vin, OCP < Iout, Vin == Vout (needs a topology
     that can only be decided by user → question), tj_max default gets an
     assumptions entry.
- Acceptance: new unit tests for each pattern; old tests untouched.

### B2. Phase 22: wire the bundle into finalize
- Files: `orchestrator/graph.py` (`finalize`), `bundle/manifest.py`.
- Steps:
  1. In `finalize`, call `build_manifest_from_artifacts(...)` /
     `write_bundle(...)` (check exact signatures) into
     `runs/<run>/bundle/manifest.json`; include requirements (with protection
     fields), components incl. ICs from A2, caveats, validation status.
  2. Include `final["caveats"]` in the manifest (verdict integrity, §6.3).
- Acceptance: every orchestrated run leaves `bundle/manifest.json`; unit test
  with mock artifacts asserts the keys.

### B3. Phase 18: provenance logging
- File: `orchestrator/graph.py`.
- Steps:
  1. Send `temperature: 0` (or configured sampling) in the OpenRouter payload.
  2. Append per-call records to `runs/<run>/llm_provenance.jsonl`:
     `{step, model, temperature, prompt_messages, response, tool_calls}`.
     Keep files on disk (tool contract: no raw dumps in state).
  3. Update CLAUDE.md claim to match reality.
- Acceptance: a mock-driven run writes the jsonl with one line per reason step.

### B4. Phase 20: HITL gates — decide, then implement or descope formally
- Current state: stubs were removed (commit `55eafcc`); nothing asks the user.
- Decision needed (pick one):
  - **Minimal implementation**: a `pending_approval.json` in the run dir +
    UI banner with Approve/Reject for the two cheapest gates: (1) first CHT
    solve of a new design, (2) Tj-discrepancy gate (reduced vs CHT > margin).
    Agent pauses (state "awaiting_approval") until answered or timeout.
  - **Formal descope**: write the decision + rationale into
    `updated_project_goals.md` §Phase 20 and CLAUDE.md so the goals doc stops
    promising it.
- Acceptance: either a UI-interruptible approval flow with a test, or the docs
  updated and a note in this file marking it descoped.

### B5. Phase 4: Design as source of truth (incremental, not a rewrite)
- Files: `orchestrator/tools.py`, `design/object.py`.
- Steps (pragmatic slice):
  1. Create ONE `Design` in `tool_size_converter` (or a `tool_start_design`)
     and hold it on `ToolContext`; populate requirements (+vin range from B1),
     topology, parameters, selected components (A2), simulation config.
  2. `graph.finalize` serializes it to `runs/<run>/design.json` (it already
     round-trips via `to_dict/_from_dict` with `schema_version`).
  3. Manifest (B2) references the design file. Netlist/BOM/manifest divergence
     now has one anchor to compare against.
  4. Full regeneration-from-Design remains future work — note it.
- Acceptance: smoke run leaves `design.json` matching the artifacts; reload
  test (`_from_dict`) passes on it.

---

## Group C — Polish / small items

### C1. Phase 11: expose mesh fidelity (already built)
- File: `orchestrator/tools.py` — add `fidelity` param ("fast"|"balanced"|"high",
  default "balanced") to the `run_thermal` tool schema, pass to `build_case`.
- Acceptance: UI/agent can pick fast; cell-count note in payload.

### C2. Phase 2/3 (follow-on): worst-case-corner design across a Vin range
- With `vin_min/vin_max` from B1: size L/C at the worst endpoint per topology
  (buck: max Vin; boost: min Vin), screen at both corners, and note the
  corner in the design. (Engine still single-Vin — keep it that way; run it
  twice.)
- Acceptance: "Vin = 36-60 V" spec produces bounds + corner note in smoke.

### C3. Phase 17: UI editing (medium effort — schedule deliberately)
- File: `ui/app.py`.
- Minimum viable per goals: component-value editor (pick alternates from
  library queries for MOSFET/inductor/cap — never free-text part numbers),
  re-run button, run-comparison table (two runs side by side: eff, Tj,
  ripple, parts), waveform probe (hover readout on the ripple plot).
- Node connect/disconnect editing is a large feature — descope with a note
  unless genuinely needed.
- Acceptance: can swap the inductor choice from the UI and re-run end-to-end.

### C4. Phase 23: named regression cases
- Files: `tests/unit/` (+ `scripts/topology_smoke.py --case`?).
- Add explicit cases from the goals list: light load (0.25×Iout), heavy load
  (stress), thermal failure (force tiny airflow → mitigation path), infeasible
  spec (screening rejection), component substitution (forced alternate part),
  transient (once A3 lands). Mock or env-gate the expensive ones.
- Acceptance: `pytest tests/` names map 1:1 to the goals' case list.

### C5. Doc alignment (quick)
- CLAUDE.md: remove/adjust claims — gate-driver/controller/diode libraries
  (until A1), Jinja2 `templates/` workflow (mesh is programmatic; jinja2 was
  removed from deps per AUDIT §5), "Model + temperature logged" (until B3),
  baseline numbers (refresh after each group).
- `PROJECT_STRUCTURE.md` / `dc-dc-synthesizer-phase-plan.md` Implementation
  Status section: sync with this file, then tick items there as they land.

---

## Group D — Long-term / descoped (record, don't start now)

- **Phase 15 parallel evaluation**: serial NSGA-II is fast enough at current
  population sizes; revisit only if sweeps (A4) + Pareto (A5) get composed.
  A `concurrent.futures` pool over candidate evaluation is the natural design.
- **Phase 21 hardware validation**: needs a physical bench (efficiency,
  thermocouples, scope). Nothing to build in software until hardware exists;
  the manifest/validation plumbing from B2 is the only prep worth doing.
- **Transient thermal**: explicitly out of scope per Phase 12 scope decision.
- **Placement optimization (Phase 10 advanced)**, **tolerance/temperature
  sweeps (Phase 8 advanced)**: only after A4 exists and only if a use case
  demands them.

---

## Verification block (run after every group)

```bash
# host (fast, no OpenFOAM): expect 151 passed / 7 env-gated skips baseline
python -m pytest tests/

# canonical container: expect 158 passed / 0 failed
bash scripts/dtest.sh

# end-to-end per topology (add --thermal inside the container for full CHT)
python scripts/topology_smoke.py

# docs stay truthful: re-check CLAUDE.md claims after each group
```

Update this file as items land: move them to a "DONE" list below, refresh the
status table, and re-date the baseline line at the top.

## DONE log (append as you complete)

- **2026-09-19 — Group A complete.** New files: `library/data/{gate_drivers,
  controllers,diodes}.yaml`, `spice/step_tests.py`, `spice/sweeps.py`,
  `tests/unit/test_group_a.py`. Modified: `library/loader.py` (3 query
  helpers), `netlist/selector.py` (IC selection), `spice/losses.py` (cap-ESR
  loss + zero-gate-current guard in `crossover_time`), `optimization/pareto.py`
  (shared clamped crossover, collection-phase re-screening + correct
  v_block/conduction factor), `orchestrator/tools.py` (`run_step_tests`,
  `run_sweeps`, `optimize_pareto` tools + dispatch + schemas; build_netlist
  added to pareto-finalist AND mitigation child pipelines),
  `orchestrator/graph.py` (SYSTEM_PROMPT lists the new tools), `ui/app.py`
  (step/sweep/pareto panels). Host: **179 passed / 6 skips**, smoke ALL
  PASSED. Still to do: in-container `dtest.sh` + smoke `--thermal` (finalist
  CHT verification only runs there), then commit. `test_p1_new_categories_
  optional` rewritten to a stripped-temp-dir basis (old form asserted the IC
  files don't exist).
- (2026-09-17 snapshot taken at 158/158 in-container)
