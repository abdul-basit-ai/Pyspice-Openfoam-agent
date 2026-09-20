# remain_plan.md — Phase Completion Status & Remaining Work

Created: 2026-09-17 (after commits through `0af71c1`, post-AUDIT.md).
**Updated 2026-09-20: Groups A-D + the completeness-driven hardening pass** (see DONE
log). Host suite count re-pinned after the library rework; `topology_smoke.py` ALL
PASSED (buck / boost / buck_boost, host tier). **In-container re-verification DONE (2026-09-20):** smoke `--thermal`
ALL PASSED for all three topologies — the reworked CHT physics (real FR4/Si
materials, wind-tunnel envelope) converges at relaxation level 0 and yields
physically plausible Tj (buck 37.4C, buck_boost 45.6C @ 2.27 W; R_eff ~9 K/W,
vs ~2.3 K/W for the old aluminum-slab model). The reduced-vs-CHT gap is
quantified per run (4.3 / 12.7 degC). In-container pytest after fixes: **215 passed / 1 env-dependent
assertion** (the Group D finalist test asserted the HOST outcome; in the
container the CHT verification genuinely succeeds — made environment-aware;
effective 216/216).

How to use: work top-to-bottom by priority group. Each item lists the gap, the
file(s) to touch, concrete steps, and an acceptance check. Tick items off as
you go. Re-run the verification block at the bottom after every group.

---

## Status summary (vs `updated_project_goals.md` Phases 0–23)

| Phase | Area | Status | What's missing (one line) |
|---|---|---|---|
| 0 | Environment & reproducibility | **DONE** | sampling settings now sent+logged (B3); keep host `pip install -e .` fresh (pymoo was missing once); in-container baseline number needs a re-pin after `dtest.sh` |
| 1 | Component database | **PARTIAL → A1 data done** | gate drivers (3) / controllers (4) / diodes (5) shipped; Coss/Crss still unverified per part (see A1 notes); core-loss fields remain descope-by-documentation |
| 2 | Spec parser & feasibility | **DONE (B1+C2)** | input ranges with two-corner sizing in size_converter, MHz/Hz, V-ripple, efficiency phrasings, UVLO/OCP/equal-V contradiction checks, tj default documented |
| 3 | Topology + sizing | **DONE** | (multi-criteria scorer is advisory-only — acceptable, documented) |
| 4 | Unified Design object | **DONE (B5 incremental)** | Design created in size_converter, populated per stage, serialized to runs/<run>/design.json by finalize; regeneration-from-Design = future work |
| 5 | Selection + schematic | **DONE (A2+C3)** | IC selection + library-validated part overrides with margin audit |
| 6 | Control loop | **DONE (A3, open-loop scope)** | step tests shipped (`run_step_tests`); closed-loop SPICE step testing documented as follow-on |
| 7 | Fast screening | **DONE** | (ripple prediction + banking + safety screens all in) |
| 8 | SPICE + steady state | **PARTIAL → A4 load+vin sweeps done** | temperature/tolerance sweeps remain descoped (documented in the tool) |
| 9 | Loss extraction | **DONE (A6)** | cap-ESR loss computed + reported; Vf/core-loss recompute stays data-blocked |
| 10 | Board geometry | **DONE** | (placement-as-optimization-variable = advanced, descoped) |
| 11 | Mesh + case generation | **DONE (C1)** | fidelity preset exposed via `run_thermal` (fast/balanced/high) |
| 12 | CHT solve | **DONE** | (steady-state only, by documented scope decision) |
| 13 | Extraction + validation | **DONE** | — |
| 14 | Multi-objective optimization | **DONE (A5)** | `optimize_pareto` tool + CHT finalist verification; reduced-tier bugs fixed |
| 15 | Parallel exploration | **DONE (D1)** | bounded PROCESS pool (`orchestrator/parallel.py`) behind `optimize_pareto.max_workers`; per-candidate isolation + error capture + serial fallback |
| 16 | Feedback / failure diagnosis | **DONE** | (auto-mitigation + caveats; HITL ask-before-change descoped) |
| 17 | Interactive UI | **DONE (C3 minimum-viable)** | library-only part-override editor + input-range fields + Compare tab + re-run; waveform probe + node editing descoped (see flags) |
| 18 | AI orchestrator | **DONE (B3)** | temperature=0 sent; every real call appends model/prompt/response to runs/<run>/llm_provenance.jsonl |
| 19 | Memory / knowledge base | **DONE** | — |
| 20 | HITL approval gates | **DESCOPED (B4, documented)** | status note added to goals doc Phase 20: best-effort contract + caveats/banners/provenance cover the intent; revisit only for concurrent operators |
| 21 | Hardware validation | **DESCOPED (D2, documented)** | goals-doc status note: nothing built until a bench exists; manifest.json is the sim-side import point for measured results |
| 22 | Unified output package | **DONE (B2)** | finalize writes schema-valid manifest.json (feasible/infeasible/error) incl. ICs, protection posture, verification summaries, caveats |
| 23 | Regression suite | **DONE (C4)** | all goals-named cases mapped: light/heavy load + substitution e2e (gated), mitigation gating, infeasible spec (hard) + beyond-library (loud), transients (Group A step tests) |

Doc status (C5/D): CLAUDE.md is aligned again (IC libraries EXIST since A1;
Jinja2-template claim corrected — blockMeshDict is programmatic; temperature/
provenance claim TRUE since B3; parallel.py documented in D; baselines
refreshed: host 210/6, container number to be re-pinned after the next
`dtest.sh`). PROJECT_STRUCTURE.md updated for the new modules.

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

## Group B — Contract / policy gaps — **IMPLEMENTED 2026-09-19**

> What shipped (host: **191 passed / 6 CHT-gated skips**, smoke ALL PASSED;
> new tests in `tests/unit/test_group_b.py`):
>
> - **B1 parser** (`sizing/spec_parser.py` + `Requirements.vin_min/vin_max`):
>   input ranges in all three phrasings ("Vin = 36-60 V", "input 12-36 V",
>   "12-36 V input", incl. "to") — both endpoints recorded, design point =
>   worst-case voltage-stress endpoint (max) with a loud assumption + a
>   missing-material entry; MHz/kHz/plain-Hz fsw; V-unit ripple (mV still
>   wins); efficiency gap widened to 24 chars + number-first form ("95%
>   efficiency"); "junction temperature" alias; NEW contradiction checks:
>   empty range, Vin==Vout, UVLO > input max, OCP <= Iout; tj_max default
>   now a documented assumption (150 °C = library max, zero margin).
>   Schema-additive — old design.json files load unchanged.
> - **B2 manifest** (`bundle/manifest.py` + `graph.finalize`): every
>   orchestrated run writes `runs/<run>/manifest.json` — status
>   feasible/infeasible/error, component list incl. gate driver/controller/
>   diode, spec incl. protection posture + input range + unresolved safety
>   items, control/step/sweep/electro-thermal summaries in `electrical`,
>   CHT validation evidence in `thermal`, caveats in `notes`.
> - **B3 provenance** (`graph._call_openrouter`): `temperature: 0` in the
>   payload; every real (non-mock) call appends {ts, step, model,
>   temperature, usage, request_messages, response} to
>   `runs/<run>/llm_provenance.jsonl` (best-effort; mock replays are tests,
>   not design decisions). CLAUDE.md claim now true.
> - **B4 HITL descope (documented default)**: status note added to
>   `updated_project_goals.md` Phase 20 — best-effort contract + caveats
>   banners + manifest/provenance cover the gates' intent; the minimal
>   interactive version (pending_approval.json + UI banner) is sketched
>   there for a future revisit.
> - **B5 Design persistence** (`ToolContext.design`): Design created in
>   `tool_size_converter` (requirements + topology + rationale + sizing
>   parameters), components populated in `select_components` (incl. ICs with
>   datasheet URLs), netlist/thermal paths attached, serialized by
>   `finalize` to `runs/<run>/design.json` (schema-version-checked reload
>   pinned by test). Regeneration-from-Design = future work.

### B1–B5 detail (original plan, kept for reference)


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

## Group C — Polish / small items — **IMPLEMENTED 2026-09-19**

> What shipped (host: **205 passed / 6 CHT-gated skips**, smoke ALL PASSED;
> new tests in `tests/unit/test_group_c.py`):
>
> - **C1 fidelity:** `run_thermal` takes `fidelity` (fast/balanced/high) and
>   passes it to `build_case`; unknown presets fail loudly.
> - **UI one-liner:** Results tab reads `crossover_hz` (the old
>   `crossover_kHz` key never matched — the metric always showed N/A).
> - **C2 two-corner sizing:** `size_converter` accepts `vin_min`/`vin_max` —
>   sizes at BOTH corners, keeps the worst of each stress quantity, pins the
>   design point at the voltage-stress endpoint, records the range on
>   Design.requirements + rationale; a topology flip across the range
>   (buck at one end, boost at the other) forces the 4-switch buck_boost.
>   Lonely endpoint is an error.
> - **C3 UI (minimum-viable editing):** sidebar gains an input-range pair and
>   three library-populated part-override dropdowns (free-text part numbers
>   are IMPOSSIBLE in the UI — the hallucination guardrail is structural);
>   the scripted mode forwards ranges + overrides through the tool layer; a
>   fifth **Compare** tab puts any runs side by side (status, parts, eff,
>   ripple, loss, margins, Tj max). Re-run = the Run button with new
>   sidebar state. `select_components` gained `mosfet`/`inductor`/
>   `capacitor` override args: library-validated, margin-audited (misses
>   surface as warnings; hard safety screens still reject violations).
>   Fixed en route: dispatch dropped all `select_components` args.
> - **C4 named regression cases:** light load (0.25x) + heavy load (4x) +
>   substitution e2e (real ngspice, gated), mitigation tool gating,
>   hard-infeasible spec (150 V input -> no MOSFET) and the
>   ripple-beyond-library case pinned to the deliberate best-effort contract
>   (loud note, measured gate decides). Coverage map in the test module
>   docstring matches the goals Phase 23 list 1:1.
> - **C5 docs:** CLAUDE.md + PROJECT_STRUCTURE.md aligned; baselines
>   refreshed.
>
> Descopes (deliberate, see Group C flags): waveform hover-probe,
> node connect/disconnect editing, temperature/tolerance sweeps.

### C1. Phase 11: expose mesh fidelity (already built) — DONE
- File: `orchestrator/tools.py` — add `fidelity` param ("fast"|"balanced"|"high",
  default "balanced") to the `run_thermal` tool schema, pass to `build_case`.
- Acceptance: UI/agent can pick fast; cell-count note in payload.

### C2. Two-corner range sizing — DONE
- With `vin_min/vin_max` from B1: size L/C at the worst endpoint per topology
  (buck: max Vin; boost: min Vin), screen at both corners, and note the
  corner in the design. (Engine still single-Vin — keep it that way; run it
  twice.)
- Acceptance: "Vin = 36-60 V" spec produces bounds + corner note in smoke.

### C3. Phase 17: UI editing — DONE (minimum-viable)
- File: `ui/app.py`.
- Minimum viable per goals: component-value editor (pick alternates from
  library queries for MOSFET/inductor/cap — never free-text part numbers),
  re-run button, run-comparison table (two runs side by side: eff, Tj,
  ripple, parts), waveform probe (hover readout on the ripple plot).
- Node connect/disconnect editing is a large feature — descope with a note
  unless genuinely needed.
- Acceptance: can swap the inductor choice from the UI and re-run end-to-end.

### C4. Phase 23: named regression cases — DONE
- Files: `tests/unit/` (+ `scripts/topology_smoke.py --case`?).
- Add explicit cases from the goals list: light load (0.25×Iout), heavy load
  (stress), thermal failure (force tiny airflow → mitigation path), infeasible
  spec (screening rejection), component substitution (forced alternate part),
  transient (once A3 lands). Mock or env-gate the expensive ones.
- Acceptance: `pytest tests/` names map 1:1 to the goals' case list.

### C5. Doc alignment — DONE
- CLAUDE.md: remove/adjust claims — gate-driver/controller/diode libraries
  (until A1), Jinja2 `templates/` workflow (mesh is programmatic; jinja2 was
  removed from deps per AUDIT §5), "Model + temperature logged" (until B3),
  baseline numbers (refresh after each group).
- `PROJECT_STRUCTURE.md` / `dc-dc-synthesizer-phase-plan.md` Implementation
  Status section: sync with this file, then tick items there as they land.

---

## Group D — Long-term / descoped — **RESOLVED 2026-09-20**

> What shipped (host: **210 passed / 6 CHT-gated skips**; new tests in
> `tests/unit/test_group_d.py`):
>
> - **D1 Phase 15 (the one implementable item):** `orchestrator/parallel.py`
>   — bounded PROCESS-parallel evaluation of independent candidates, wired
>   behind `optimize_pareto(max_workers=2)`. Processes are mandatory (one
>   shared ngspice instance per process — threads would interleave circuits);
>   each finalist gets its own process/library/instance/run dir, worker
>   failures are captured per candidate, pool-startup failure degrades to
>   serial, results return in input order. The NSGA-II reduced-order inner
>   loop stays serial by design (microseconds per candidate). Verified live:
>   2 finalists in 2 processes, SPICE completed in both, per-finalist dirs on
>   disk, CHT failed only with the honest container-only reason.
> - **D2 Phase 21:** formal goals-doc status note — deferred until hardware;
>   the planned import path is manifest.json + a future measured_results.json
>   sidecar + a deterministic sim-vs-measured comparison feeding Phase 19.
> - **D3 remaining descopes formally recorded in the goals doc:** Phase 8
>   temperature/tolerance sweeps (rig has no temperature-dependent models),
>   Phase 10 placement-as-optimization-variable (would break JEDEC
>   comparability), Phase 12 transient thermal (pre-existing scope decision).
>   The old `parallel/` stub-package note in PROJECT_STRUCTURE.md corrected.
>
> Original Group D plan (kept for reference):

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

- **2026-09-20 — completeness-driven hardening pass** (critical+important
  items from the project rating):
  * MOSFET catalog rebuilt ALL-Texas-Instruments (6 CSD NexFET parts,
    25/40/60/100 V classes) — every value extracted programmatically from
    the TI datasheet PDFs (pypdf), Coss/Crss/Qrr now populated on every
    part; the unverifiable SISS44DN10 and all Vishay parts removed
    (critical #2 + Coss important item). V_plateau estimates documented
    (TI does not tabulate plateau; Vth-max + headroom, conservative).
  * Diodes: Vishay SS34/SS54 removed (3 trusted-maker parts remain);
    Inductors: 3 Vishay IHLP entries removed.
  * screening.py made topology-aware (boost blocks Vout and carries
    I_L=Iout/(1-D); cap-ripple swing, conduction floor and Tj floor all on
    the real basis) — important item.
  * Reduced-vs-CHT Tj discrepancy quantified per run in artifacts and
    flagged > 15 degC (important item).
  * CI: GitHub Actions workflow (host suite + smoke on push/PR); MIT
    LICENSE added (important items).
  * scripts/matrix_test.py: topology x ripple-ratio x fsw coverage matrix
    through the real tool chain (user acceptance request). Two 250 kHz
    corners exceed the library's verified inductor coverage and fail
    loudly with the exact requirement — documented, not papered over.
  * Closed-loop SPICE rig: explored and BLOCKED — shared-library
    ngSpice_Circ strips LAPLACE/B braces and LAPLACE transient evaluation
    hangs; findings recorded in the goals doc (important item, honest
    partial).
- **2026-09-20 — Group D complete.** Phase 15 implemented as bounded
  process parallelism (orchestrator/parallel.py + optimize_pareto
  max_workers, module-level finalist worker for spawn picklability);
  Phase 21 + tolerance/placement/transient-thermal descopes formally
  recorded in updated_project_goals.md; CLAUDE.md + PROJECT_STRUCTURE.md
  aligned. Host: **210 passed / 6 skips**. Tests: tests/unit/test_group_d.py
  (5, incl. a real 2-process finalist e2e).
- **2026-09-19 — Group C complete.** run_thermal fidelity param; crossover
  UI key fix; two-corner range sizing (vin_min/vin_max) incl. the
  topology-flip -> buck_boost rule; select_components part overrides
  (library-validated + margin audit; dispatch arg-drop bug fixed); UI
  sidebar input-range + part-override dropdowns + Compare tab; goals-named
  regression cases (tests/unit/test_group_c.py, 14); CLAUDE.md +
  PROJECT_STRUCTURE.md aligned, baselines refreshed. Host: **205 passed /
  6 skips**, smoke ALL PASSED.
- **2026-09-19 — Group B complete.** Parser units/ranges + Requirements
  vin_min/vin_max; manifest wired into finalize (ICs, protection posture,
  verification summaries, caveats); temperature=0 + llm_provenance.jsonl on
  every real LLM call; HITL gates formally descoped in the goals doc (B4,
  documented default); Design object created/populated per stage and
  serialized to design.json. CLAUDE.md: Jinja2 claim corrected, temperature
  claim now true, orchestrator entry lists the new tools + run outputs.
  Host: **191 passed / 6 skips**, smoke ALL PASSED. Tests:
  `tests/unit/test_group_b.py` (12).
- **2026-09-19 — Group A complete.** Committed as `ed5bf2a`. New files:
  `library/data/{gate_drivers,controllers,diodes}.yaml`, `spice/step_tests.py`,
  `spice/sweeps.py`, `tests/unit/test_group_a.py`. Modified:
  `library/loader.py` (3 query helpers), `netlist/selector.py` (IC selection),
  `spice/losses.py` (cap-ESR loss + zero-gate-current guard in
  `crossover_time`), `optimization/pareto.py` (shared clamped crossover,
  collection-phase re-screening + correct v_block/conduction factor),
  `orchestrator/tools.py` (`run_step_tests`, `run_sweeps`, `optimize_pareto`
  tools + dispatch + schemas; build_netlist added to pareto-finalist AND
  mitigation child pipelines), `orchestrator/graph.py` (SYSTEM_PROMPT lists
  the new tools), `ui/app.py` (step/sweep/pareto panels). Host: **179 passed /
  6 skips**, smoke ALL PASSED. `test_p1_new_categories_optional` rewritten to
  a stripped-temp-dir basis (old form asserted the IC files don't exist).
- (2026-09-17 snapshot taken at 158/158 in-container)

---

## FLAGS — per group (read before working a group)

### Group A flags (implemented 2026-09-19, commit `ed5bf2a`)
- ⚠️ **In-container verification still pending**: finalist CHT verification
  (`optimize_pareto`) and the full `dtest.sh` + smoke `--thermal` only run
  in the Docker container; host run can't exercise them. Needs `runs/` disk
  headroom.
- ⚠️ **Data integrity**: `SISS44DN10` (mosfets.yaml) could not be verified as
  a real Vishay part — closest real part is SiSS4410DN (40 V, Coss 168 pF /
  Crss 20 pF); its datasheet URL 404s. Audit and fix the whole record.
- 📋 Coss/Crss still unset on all 12 MOSFETs — add only datasheet-verified
  values (switching loss honestly omits the term while absent).
- 📋 Inductor core-loss (Steinmetz) fields remain None — documented descope.
- 📋 Step tests measure the OPEN-LOOP plant (rig has no feedback loop).
  Closed-loop SPICE step testing (Type III network or Laplace behavioral
  source in ngspice) is a possible follow-on; current loop validation is
  margins-only on the averaged model.
- 📋 Load-sweep `efficiency_waveform` is an upper bound (ideal switches carry
  no modeled switching loss); `run_spice`'s loss-model efficiency stays the
  design number. At light load it can clamp to 1.0.
- 📋 Host venv had a stale install (pymoo declared but absent) — keep
  `pip install -e .` fresh after pulling; container is canonical.

### Group B flags (contract / policy gaps) — implemented 2026-09-19
- 📋 B4 took the DOCUMENTED DEFAULT (descope) — if you want the interactive
  approval gate later, the sketch is in the goals-doc Phase 20 status note
  (pending_approval.json + UI banner + first-CHT/Tj-discrepancy gates).
- ⚠️ Range specs design at the WORST-CASE-VOLTAGE endpoint only (max Vin).
  For boost designs the harder corner is MIN Vin (higher duty/current) —
  C2 (two-corner sizing + screening at both endpoints) is still open and
  matters most for boost/buck_boost range specs.
- 📋 design.json population is best-effort per stage; a run that errors mid-
  pipeline still serializes whatever it had (netlist_path/components may be
  None) — intentional (checkable-output contract), not a bug.
- 📋 llm_provenance.jsonl only logs REAL OpenRouter calls (mock test replays
  are excluded by design); a live run without OPENROUTER_API_KEY fails before
  any record is written — the error itself lands in RunState, not provenance.
- 📋 Manifest `status` semantics: converged CHT = "feasible" even with
  caveats (caveats ride in notes); no-thermal-but-parts = "infeasible";
  pre-sizing error = "error".

### Group C flags (polish) — implemented 2026-09-19
- ✅ The `crossover_kHz` UI bug is FIXED (reads `crossover_hz`, renders kHz).
- 📋 C3 descopes (deliberate): waveform hover-probe (st.image is a static
  PNG — interactive probing needs a plotly/altair rework of the waveform
  render) and node connect/disconnect editing (the netlist is generated,
  not hand-drawn; editing nodes means editing the builder). Both revisited
  only if a concrete need appears.
- ⚠️ Two-corner sizing merges per-quantity WORST values; the duty/electrical
  seed is taken at the PINNED corner (vin_max). The servo re-trims duty at
  simulation time, so corner mismatch cannot poison the rig — but reported
  `duty_cycle` describes the vin_max corner only (per-corner duties are in
  the sizing notes).
- 📋 UI overrides only cover MOSFET/inductor/capacitor; gate-driver/
  controller substitution would need the same pattern in select_*_ic —
  trivial to add when wanted.
- 📋 Compare tab reads state.json snapshots; a run in flight shows blanks
  for stages it has not reached (expected).
- 📋 In-container baseline (was 158 at audit time) needs one `dtest.sh` run
  to re-pin in CLAUDE.md — host count is authoritative between container
  runs.

### Group D flags — resolved 2026-09-20
- 📋 Parallel finalists each re-run the FULL child pipeline (sizing →
  selection → servo'd SPICE → CHT): 2 workers ≈ 2 CHT solves in parallel.
  Raising max_workers beyond available cores gains nothing (CHT itself is
  serial per case) — keep 2-3 in the container.
- 📋 Phase 21 revisit recipe is written in the goals-doc note
  (measured_results.json sidecar + comparison module → Phase 19 memory);
  do not build it speculatively before hardware exists.
- 📌 Transient thermal stays OUT OF SCOPE (Phase 12 decision) — no later
  phase may assume transient thermal data exists.
