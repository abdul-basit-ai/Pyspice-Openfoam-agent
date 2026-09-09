# DC-DC Synthesizer — Phase-by-Phase Build Plan

*A sequential, independently-testable build plan for the PySpice + OpenFOAM converter design agent, with reasoning behind every non-obvious design choice and sources for the ones that matter most.*

---

## How to Use This Document

Each phase is self-contained: it has a goal, the reasoning for *why* it's built this way (not just "the common way"), concrete tasks, and a **standalone checkpoint** — something you can run and verify before touching the next phase. Phases are ordered so that each one only depends on phases above it. Where a design choice rests on a specific external fact (a standard, a library's actual capabilities, a published method), the source is listed so you can verify it yourself rather than take my word for it.

## Phase Dependency Map

```text
Phase 0  Environment & Reproducibility
   │
Phase 1  Component Library & Data Schema
   │
Phase 2  Feasibility Check + Topology Classifier + Sizing Engine
   │
Phase 3  Netlist Builder + Component Selection
   │
Phase 4  PySpice Transient Loop + Steady-State Detection
   │
Phase 5  Loss Extraction Layer (Conduction + Switching)
   │
   ├──────────────────────────┐
   │                          │
Phase 6  JEDEC Board Template │
   │                          │
Phase 7  Mesh/Case Generator  │
   │                          │
Phase 8  CHT Solve Automation │
   │                          │
Phase 9  Result Extraction    │
   │                          │
Phase 10 Thermal Mitigation Loop (closes back to Phase 2/3/5)
   │
Phase 11 LLM Orchestrator (LangGraph tool-loop, ReAct-style reasoning)
   │
Phase 11a Memory Layer (short-term run state + long-term design memory)
   │
Phase 11b HITL Approval Gates (LangGraph interrupt + Streamlit/CLI resume)
   │
Phase 11c Parallel Design-Space Evaluation (fan-out thermal cases)
   │
Phase 12 Unified Output Bundle
   │
Phase 13 Cross-Phase Validation & Regression Suite
   │
Phase 14 (Optional) Tier-1 Foster/Cauer Fast Screen
```

---

## Phase 0 — Environment & Reproducibility Foundation

**Goal:** A pinned, containerized environment with ngspice, OpenFOAM, and Python dependencies at known versions, runnable identically on any machine.

**Why this matters (not just "nice to have"):** The agent will run hundreds of simulations autonomously over the project's life. If ngspice or OpenFOAM silently update and change numerical defaults (relaxation factors, solver tolerances), old results become non-reproducible and debugging becomes guesswork. OpenFOAM also has two independently-versioned distributions — the ESI/openfoam.com line and the OpenFOAM Foundation/openfoam.org line — whose case-file formats have diverged over time; several public chtMultiRegionFoam tutorials explicitly note they only work on one line and not the other. Pin one distribution and one version, not "OpenFOAM" generically.

**Design choice:** Use **Docker Engine (CLI) or Podman**, not Docker Desktop. Both are fully open-source and free with no commercial-use licensing question, whereas Docker Desktop's free tier has usage restrictions for larger organizations — an unnecessary risk for a "free tools only" project even if you're currently a single user.

**Tasks:**
- Dockerfile pinning: OpenFOAM (pick one distribution + version, e.g. openfoam.com v2406+), ngspice, Python 3.x, PySpice, langgraph, langchain-core, foamlib, PyVista, Jinja2.
- `requirements.txt` with pinned versions.
- A `Makefile` or shell script that builds and runs the container reproducibly.

**Standalone checkpoint:** Inside the container, run the stock OpenFOAM `cavity` tutorial and a trivial ngspice `.cir` file (e.g. an RC divider) end-to-end. Both should complete without manual intervention.

**Key references:**
- OpenFOAM tutorial compatibility note across distributions/versions — [CFD-Training chtMultiRegionFoam tutorial](https://cfd-training.com/produit/openfoam-tutorial-fan-fins-conjugate-heat-transfert/) (explicitly flags version incompatibility between the two OpenFOAM lines).

---

## Phase 1 — Component Library & Data Schema

**Goal:** A static, curated, version-controlled library of real MOSFETs, inductors, and capacitors, each with electrical *and* thermal *and* package metadata.

**Why this design (vs. a distributor API):** The original proposal suggested pulling live part data from distributor APIs (Octopart/Nexar, Digi-Key). Given the free-tools constraint, that's the wrong call even ignoring cost — those APIs are rate-limited or require paid tiers at any real query volume, and introduce a runtime dependency that can silently fail mid-agent-run. A static curated dataset built once from public manufacturer datasheets is slower to expand but has zero runtime cost, zero external dependency, and is fully inspectable/versionable in Git.

**Design choice — schema fields matter more than volume:** Don't just carry the fields the original proposal listed (`Vds_max`, `Rds_on`, `Qg`, package dims, `Rθjc`, `Rθja`). Also carry **`Qgd`, `V_plateau`, and `Ciss`** — without them, Phase 5's switching-loss model has no way to estimate turn-on/turn-off crossover time, and switching loss is frequently comparable to or larger than conduction loss at the switching frequencies this project targets.

```python
MOSFET_LIBRARY = {
    "BSC014N04LS": {
        "Vds_max": 40.0, "Rds_on": 0.0014, "Qg": 49e-9,
        "Qgd": 9.5e-9, "V_plateau": 4.5, "Ciss": 3900e-12,
        "package": "PG-TDSON-8",
        "die_x_mm": 5.15, "die_y_mm": 6.0, "die_z_mm": 1.0,
        "R_theta_jc": 0.8, "R_theta_ja": 50.0
    }
}
```

**Tasks:**
- Define the YAML/JSON schema (MOSFETs, inductors with ESR/DCR/Isat, output capacitors with ESR).
- Populate 10–20 real parts per category from public datasheets.
- Write a loader + query function (filter by voltage/current rating).

**Standalone checkpoint:** Load the library, query "MOSFETs rated ≥ 40V, Rds_on < 5mΩ," confirm correct filtering against a hand-checked answer.

**Key references:**
- Datasheet parameter extraction methodology (Qgd, plateau voltage, Ciss/Crss from datasheet curves) — [MCC Semi, "Quick Guide for Power Losses Calculation in MOSFETs – Part 2"](https://solutions.mccsemi.com/news/application-note-quick-guide-for-power-losses-calculation-in-mosfets-part-2).

---

## Phase 2 — Feasibility Pre-Check + Topology Classifier + Analytical Sizing Engine

**Goal:** Validate the input spec is physically achievable, pick the topology (buck/boost/buck-boost), and compute baseline `L` and `Cout` from first-principles equations.

**Why a pre-check is a separate step, not folded into the classifier:** The original pipeline goes straight from input to topology classification. That silently accepts impossible specs (e.g., `Vout > Vin` requested for a pure-buck-only constraint, or negative/zero values) and only fails downstream — after burning a SPICE run. A pre-check is a five-line function; skipping it isn't simplicity, it's a missing input gate.

**Design choice — sizing equations, and why these specific ones:** Use volt-second balance for inductor sizing and the capacitor charge-balance / ESR-dominated ripple equation, per the standard treatment in Erickson & Maksimović. For a buck converter, the canonical forms are:

$$L_{min} = \frac{(1-D)\,V_{out}}{2\,f_{sw}\,I_{ripple,max}} \qquad C_{min} = \frac{(1-D)\,V_{out}}{8\,L\,f_{sw}^2\,V_{ripple}}$$

These aren't "the famous ones because they're famous" — they're the correct closed-form result of applying charge/volt-second balance under the small-ripple approximation, which is the standard, textbook-verified starting point every real design house uses before SPICE refinement. The risk of *not* grounding here is inventing ad-hoc sizing heuristics that have no analytical basis and silently drift from correct behavior on edge-case specs.

**Tasks:**
- Input validator (Vin/Vout/Iout/topology sanity).
- Topology classifier (ratio-based buck/boost/buck-boost selection).
- Sizing engine implementing the equations above for each topology.

**Standalone checkpoint:** Run the sizing engine against a **published worked example** (not your own numbers) and confirm `L`/`C`/ripple match within a few percent — this is your first real regression anchor, not just a smoke test.

**Key references:**
- Erickson, R. W. & Maksimović, D., *Fundamentals of Power Electronics* — canonical source for the volt-second/charge-balance derivations used above; a course-hosted copy is available at [fmipa.umri.ac.id](https://fmipa.umri.ac.id/wp-content/uploads/2016/03/R._Erickson_Fundamentals_of_Power_Electronics_pBookZZ.org_.pdf).
- Cross-check derivation of the same buck design equations — [arXiv: "One-Quadrant Switched-Mode Power Converters"](https://arxiv.org/pdf/1607.01669), Eq. (5)–(6).
- Industry-practice version of the same equations, useful for sanity-checking your implementation — [Analog Devices, "Buck Power Stage Design Equations"](https://www.analog.com/en/resources/app-notes/buck-power-stage-design-equations.html).

---

## Phase 3 — Netlist Builder + Real Component Selection

**Goal:** Map the analytically-sized `L`/`Cout` to the *closest real part* in the Phase 1 library (with margin), and programmatically emit a valid SPICE `.cir` netlist.

**Why "closest real part with margin," not exact match:** Real inductors/capacitors come in discrete standard values (E12/E24 series) with tolerance bands (commonly ±20%). Selecting a part with margin above the analytical minimum — rather than the nearest value in either direction — avoids silently under-sizing due to tolerance stack-up, which is a documented, common practical pitfall in buck power-stage design.

**Tasks:**
- Component selector: analytical value → nearest real part with margin, from the Phase 1 library.
- Netlist template builder producing valid ngspice syntax (`.cir`).
- A lint/dry-run pass (`ngspice -b` on the file with no `.tran` executed, just netlist parse) before ever attempting a real simulation.

**Standalone checkpoint:** Feed the sizing engine's output from Phase 2's validated example, confirm the generated `.cir` parses cleanly in ngspice with zero syntax errors.

**Key references:**
- On tolerance/margin practice in real inductor selection — [passive-components.eu, "Buck Converter Design and Calculation"](https://passive-components.eu/buck-converter-design-and-calculation/) (discusses rating margin and a multi-step practical selection procedure).

---

## Phase 4 — PySpice Transient Simulation Loop + Steady-State Detection

**Goal:** Run `.tran` analysis on the generated netlist via PySpice/ngspice, and stop as soon as the circuit reaches periodic steady-state — not after an arbitrary fixed duration.

**Why PySpice specifically:** It's the most actively maintained, general-purpose open-source Python↔ngspice bridge (GPLv3, free), with a documented, examples-backed API for driving simulations and pulling results into NumPy. Narrower alternatives exist (`python-ngspice`, `py4spice`, `ngspicepy`) but are thinner wrappers with smaller communities — PySpice is the more defensible default, not the "famous" one by accident.

**Design choice — steady-state detection method:** The original doc specifies a simple heuristic: compare cycle-to-cycle average output voltage, stop when `ΔV_avg < 0.1%`. That's a reasonable *default*, but it has a known failure mode: converters with slow low-frequency dynamics (large output filters, light loading) can take many switching cycles to settle, and a naive fixed-threshold detector either runs far longer than necessary or false-triggers on a still-settling waveform. A published, more robust alternative used in commercial tools like PLECS is a **shooting-method / quasi-Newton periodic-steady-state solver**: simulate one period, measure the state error between period start and end, and use a Newton correction to jump the initial state closer to the true periodic solution, iterating a handful of times instead of thousands of raw cycles. Start with the simple threshold detector (it's correct and easy to verify), but design the interface so a shooting-method detector can be swapped in later for converters where the simple method is too slow.

**Tasks:**
- PySpice wrapper: build circuit from `.cir`, run `.tran`, extract waveforms as NumPy arrays.
- Simple cycle-to-cycle steady-state detector as the default.
- Ripple, efficiency, and transient-droop measurement functions.

**Standalone checkpoint:** Run the Phase 3 netlist, confirm the detector stops within a bounded number of cycles and that measured ripple/efficiency match the Phase 2 validated worked example within simulation tolerance.

**Key references:**
- PySpice project (GPLv3, ngspice/Xyce interface, NumPy output) — [github.com/PySpice-org/PySpice](https://github.com/PySpice-org/PySpice).
- Shooting-method / quasi-Newton periodic steady-state acceleration as used in PLECS, contrasted with naive cycle-stepping — [NSF PAR, "Converter Analysis Using Discrete Time State-Space Modeling"](https://par.nsf.gov/servlets/purl/10132884).

---

## Phase 5 — Loss Extraction Layer (Conduction + Switching)

**Goal:** From the Phase 4 waveforms, compute `P_conduction` (I²R integration) and `P_switching` (explicit crossover-time model), giving `P_loss` to hand to the thermal stage.

**Why switching loss needs an explicit model, not just "included":** The original doc states `P_loss = P_conduction + P_switching` without defining the second term — a real gap, since switching loss is often the dominant term at the switching frequencies typical of these designs and it doesn't fall out of a basic transient sweep without the right measurement setup. The standard first-order model:

$$P_{sw} \approx \tfrac{1}{2}\,V_{ds}\,I_{out}\,(t_r + t_f)\,f_{sw} + Q_{gd}\,V_{driver}\,f_{sw}$$

requires the crossover time `(t_r + t_f)`, which is estimated from `Qgd`, `V_plateau`, and the gate-drive current/resistance — this is exactly why Phase 1's schema carries those fields.

**Tasks:**
- Conduction loss: numerically integrate `I²(t)·R_on` over one steady-state period from the Phase 4 waveform.
- Switching loss: implement the crossover-time model above using library fields.
- Combine into total `P_loss`, tagged per-device (MOSFET vs. diode vs. inductor DCR loss) since the thermal stage needs a *localized* heat source, not just a lump sum.

**Standalone checkpoint:** Unit-test the loss functions against a real part's **published efficiency curve** (most MOSFET/converter IC datasheets publish an efficiency-vs-load-current curve) — back-calculate expected total loss at a known operating point and compare.

**Key references:**
- Switching loss / crossover-time derivation and the exact turn-on-loss formula used above — TI's George Lakkas, *"MOSFET power losses and how they affect power-supply efficiency,"* referenced in [TI E2E forum thread on LM5170 switching loss](https://e2e.ti.com/support/power-management-group/power-management/f/power-management-forum/887664/lm5170-mosfet-switching-loss-calculation).
- Full worked derivation including non-linear `Crss`/`Coss` handling — [EPC AN030, "Hard Switching Losses Calculations"](https://epc-co.com/epc/portals/0/epc/documents/application-notes/AN030%20Hard%20Switching%20Losses%20Calculation.pdf).

---

## Phase 6 — JEDEC Standard Board Template

**Goal:** A fixed, deterministic board geometry definition (FR4 dimensions, copper stackup, MOSFET placement zone, wind-tunnel domain) that eliminates the need for any CAD/layout generation.

**Why the JEDEC dimensions specifically, and not an arbitrary board size:** These numbers aren't arbitrary — JEDEC's JESD51-3 family standardizes them *specifically* so thermal resistance measurements are comparable across labs and vendors; using them means your simulated `Tj` numbers are directly comparable to manufacturer-published `Rθja` figures for the same parts, which is a genuinely useful cross-check you wouldn't get from a made-up board size.

**Tasks:**
- Encode the fixed board geometry (76×114mm class board, standard copper weight) as a data structure — not yet a mesh, just dimensions and zone definitions.
- Define the fluid-domain envelope (inlet, outlet, symmetry walls) as fixed constants.
- Define the MOSFET placement zone as a function of the Phase 1 library's `die_x/y/z_mm` fields.

**Standalone checkpoint:** Generate the geometry data structure for 2–3 different MOSFET packages from the library and confirm placement coordinates are computed correctly (no overlap, correct centering) — purely a data/math check, no simulation yet.

**Key references:**
- JEDEC board size and copper-thickness standardization rationale — [Electronics Cooling magazine, "JEDEC Thermal Standards: Developing a Common Understanding"](https://www.electronics-cooling.com/2019/11/jedec-thermal-standards-developing-a-common-understanding/) (states the board is at least 76mm × 114mm with ≥50µm top copper, per JESD51-3).
- Official standard family index — [JEDEC JESD51 documents](https://www.jedec.org/standards-documents/docs/jesd-51-8).

---

## Phase 7 — Parametric Mesh & Case Generator

**Goal:** Turn Phase 6's fixed geometry + Phase 1's part metadata + agent-tunable scalars (`H_fin`, `N_fin`, `v_in`) into a valid OpenFOAM case directory.

**Why Jinja2 *and* foamlib, not just Jinja2:** The original proposal used raw Jinja2 templating for the entire case, including `blockMeshDict` and `fvOptions`. That's the right tool for `blockMeshDict` — it's fundamentally a text template with computed vertex/block coordinates, and structured `blockMesh` is deliberately chosen over `snappyHexMesh` because it's guaranteed to mesh cleanly on this fixed geometry. But hand-editing scalar values (`P_loss`, `endTime`, relaxation factors) via string interpolation into `fvOptions`/`controlDict` is fragile — a stray formatting mismatch silently produces a malformed dict file that OpenFOAM may parse incorrectly or reject with an unhelpful error. **foamlib** exists specifically to solve this: it reads/writes OpenFOAM dict files as native Python dict-like objects, is actively maintained (published in *JOSS*, 2025), and is explicitly benchmarked as faster and more modern than the older PyFoam. Use Jinja2 for the one-time geometric skeleton, foamlib for every per-iteration scalar edit.

```jinja
MOSFET_Region
(
    ({{ x_min }} {{ y_min }} {{ z_board }})
    ({{ x_max }} {{ y_max }} {{ z_board + die_height }})
);
```
```python
# Per-iteration scalar edits via foamlib — no string templating risk
from foamlib import FoamCase
case = FoamCase(case_path)
case.file("constant/fvOptions")["heatSource"]["injectionRate"]["h"] = (p_loss_watts, 0)
case.control_dict["endTime"] = 1000
```

**Tasks:**
- Jinja2 template for `blockMeshDict` parameterized on board geometry + part placement.
- foamlib-based post-generation editor for `fvOptions` (heat source), `controlDict`, and boundary velocity.
- Each optimization iteration writes to a fresh case directory (`case_<n>/`), never overwrites.

**Standalone checkpoint:** Generate a case for one part, run `blockMesh` + `checkMesh` only (no solve) — confirm zero non-orthogonality/skewness errors, which structured `blockMesh` on this geometry should guarantee.

**Key references:**
- foamlib capabilities (dict-like editing, async batch case running, benchmarked vs. PyFoam) — [github.com/gerlero/foamlib](https://github.com/gerlero/foamlib); also published as Gerlero & Kler (2025), *Journal of Open Source Software*, 10(109), 7633.

---

## Phase 8 — CHT Solve Automation + Convergence Safeguards

**Goal:** Automate running the conjugate heat transfer solve on the generated case, with a timeout and relaxation-factor fallback so one bad run never hangs the agent loop.

**Why `chtMultiRegionSimpleFoam`, not `chtMultiRegionFoam` (a correction to the original proposal):** Both docs you shared specify `chtMultiRegionFoam`, which is OpenFOAM's **transient** conjugate heat transfer solver. Your actual deliverable, per the original spec, is a *converged steady-state temperature field* — for that target, `chtMultiRegionSimpleFoam` (the SIMPLE-algorithm steady-state counterpart) reaches the answer directly through iterative relaxation instead of marching through real simulated transient time to *arrive* at steady state. For a design-space search running many iterations per optimization loop, this is a meaningful, not cosmetic, speed difference — it's the same distinction as using an implicit steady solver vs. running a transient solver "long enough," and it's documented as the standard choice when the solid's transient thermal response isn't itself the thing being studied.

**Tasks:**
- Solver runner (via foamlib's async case execution) with a wall-clock timeout.
- Relaxation-factor fallback ladder: if divergence is detected, retry with more conservative under-relaxation before giving up.
- Basic Courant-number / residual sanity checks logged per run.

**Standalone checkpoint:** Run against a known public tutorial case (e.g. the OpenFOAM electronics-cooling CHT tutorial) and confirm convergence within the same order of iterations documented for that tutorial.

**Key references:**
- `chtMultiRegionSimpleFoam` defined as the steady-state CHT solver vs. `chtMultiRegionFoam` as its transient counterpart — [SimFlow, "chtMultiRegionSimpleFoam" solver documentation](https://help.sim-flow.com/solvers/cht-multi-region-simple-foam) and [SimFlow, "chtMultiRegionFoam" documentation](https://help.sim-flow.com/solvers/cht-multi-region-foam).
- Practical guidance on when the added complexity of CHT solving is actually justified, and common convergence pitfalls (`thermophysicalProperties` misconfiguration) — [CFD Pilot, "OpenFOAM Heat Transfer: buoyantSimpleFoam and chtMultiRegionFoam Setup"](https://cfdpilot.com/openfoam-heat-transfer).
- Official solver reference — [OpenFOAM documentation, chtMultiRegionFoam](https://doc.openfoam.com/2306/tools/processing/solvers/rtm/heat-transfer/chtMultiRegionFoam/).

---

## Phase 9 — Result Extraction (PyVista)

**Goal:** Programmatically pull `Tj_max` and the full temperature field out of the OpenFOAM case, with no GUI step.

**Why PyVista's native OpenFOAM reader, not `foamToVTK` + generic VTK parsing:** The original proposal's pipeline implied a manual/GUI ParaView step. PyVista ships a purpose-built `POpenFOAMReader` that reads OpenFOAM case directories directly — internal mesh and boundary patches both — without an intermediate `foamToVTK` conversion step, which is one less moving part and one less thing that can silently go stale between mesh and field data.

**Tasks:**
- `POpenFOAMReader`-based extraction script: read latest time directory, pull the solid region's temperature field.
- Extract scalar summary: `Tj_max`, `Tj_avg`, convergence status (from solver log).
- Optional: render a PNG snapshot for human review, but the agent-facing output is the scalar summary, not the image.

**Standalone checkpoint:** Run against the Phase 8 tutorial case, extract `Tj_max`, compare against the value you'd read manually in ParaView for the same case.

**Key references:**
- `pyvista.POpenFOAMReader` as the recommended OpenFOAM reading path — [PyVista documentation, "Plot OpenFOAM data"](https://docs.pyvista.org/examples/99-advanced/openfoam-example).

---

## Phase 10 — Thermal Mitigation Feedback Loop

**Goal:** Close the loop the original pipeline was missing entirely: when `Tj_max` exceeds the spec limit, decide what to change and re-enter the appropriate earlier phase.

**Why this needed to be a designed decision tree, not an afterthought:** Without this phase, the pipeline has no path to recovery on a thermal failure — it would just report "failed" and stop. That defeats the point of an *optimizing* agent. The three levers aren't equally expensive, so they should be tried in cost order:

1. **Cheapest — geometry-only:** increase `H_fin`/`N_fin` or `v_in`. Re-enters only Phase 7→9 (mesh + solve), no re-synthesis needed.
2. **Medium — component reselection:** pick a lower-`Rds_on`/lower-`Qg` part from the Phase 1 library. Re-enters Phase 3→9.
3. **Most expensive — frequency change:** lower `f_sw` to cut switching loss, which forces `L`/`C` resizing. Re-enters Phase 2→9.

**Tasks:**
- Decision function: given `Tj_max` vs. limit and iteration history, pick the next lever (start cheap, escalate only if the cheap lever plateaus).
- Iteration-count guard (don't loop forever — cap attempts and report the best result found if the spec is genuinely infeasible).

**Standalone checkpoint:** Deliberately feed an under-cooled design (e.g. natural convection only, on a high-loss part) and confirm the agent escalates through the three levers in order and either converges or correctly reports infeasibility.

---

## Phase 11 — LLM Orchestrator (LangGraph Tool-Use Agent Loop)

**Goal:** Wrap every phase above as a callable tool, and let the LLM make the high-level sequencing/escalation decisions using the ReAct-style interleaved reasoning-then-acting pattern — reason about the last tool result, decide the next action, act, observe, repeat — driven by a **LangGraph** state machine, not a hand-rolled loop.

**Why this pattern specifically:** ReAct (reason → act → observe) is the foundational, well-validated pattern for LLM tool-use agents — interleaving explicit reasoning with tool calls and observations, rather than either pure chain-of-thought (no grounding in real tool output) or pure action-without-reasoning (no ability to adapt plans on unexpected results). It's directly applicable here: the agent's job at each step *is* exactly "look at the last simulation/solve result, decide what to do next" — which is what Phase 10's decision tree already formalizes; the LLM's role is to drive that decision tree and handle the cases the hard-coded tree doesn't cover (e.g., an ambiguous or conflicting spec).

**Why LangGraph, not CrewAI and not a bare loop (a correction to earlier drafts of this plan, which said "no extra framework needed"):** That advice was right for a bare deterministic loop, but wrong given the project's actual requirements — HITL approval gates, parallel design-space evaluation, and persistence across runs. Building those by hand on a `while` loop means reimplementing checkpointing, interrupts, and thread-safe fan-out — exactly the infrastructure a mature framework already provides. The comparison that settled it:

- **CrewAI — rejected.** Its mental model is a *crew of role-based agents* (Researcher, Writer…) collaborating through conversational delegation. This pipeline is a deterministic simulation chain with *one* decision-maker driving tools — a crew adds an orchestration abstraction with no resident here, its control flow is harder to inspect than an explicit graph, and its async/parallel execution support is the weakest of the three options. Rejected despite being free/open-source.
- **Bare function-calling loop — rejected as the *final* architecture** (though it remains a fine Phase 11 development stepping-stone): it would force us to hand-build checkpointing (Phase 11a), interrupts (11b), and parallel dispatch (11c), each a source of subtle bugs the framework already solved.
- **LangGraph — selected.** Explicit graph = inspectable control flow; native `interrupt()` for HITL; checkpointer persistence for short-term state across runs; the `Send` API for parallel branches; and free/open-source. It also matches the orchestrator experience already proven in the Financial_Agentic_System project, shortening the learning curve to near zero.

**Design choice — keep the LLM's context small:** Since the LLM API is the only paid resource in this project, every tool function should return a compact structured summary (e.g. `{"Tj_max": 118.4, "converged": true, "efficiency": 0.91}`), never a raw OpenFOAM log or full VTK dump. This isn't just a cost optimization — a large irrelevant context also measurably degrades the reliability of the model's next decision.

**Tasks:**
- Define tool schemas for each phase (sizing, netlist, PySpice run, mesh gen, CHT solve, result extraction).
- LangGraph state graph: an LLM "reason" node issuing tool calls → a tool-execution node → back to reason, with the Phase 10 decision-tree levers expressed as graph edges.
- Structured logging of every tool call + LLM reasoning trace for debuggability.

**Standalone checkpoint:** Run the full orchestrator end-to-end on one simple, known-feasible spec. Verify every tool call is logged, the final result matches what you'd get running Phases 2–10 manually in sequence, and total context size stays bounded across iterations.

**Key references:**
- The reasoning-acting interleaving pattern this orchestrator implements — Yao et al., *"ReAct: Synergizing Reasoning and Acting in Language Models,"* ICLR 2023 — [arXiv:2210.03629](https://arxiv.org/pdf/2210.03629).
- LangGraph (explicit state graphs, tool calling, interrupts, persistence) — [langchain-ai.github.io/langgraph](https://langchain-ai.github.io/langgraph/).

---

## Phase 11a — Memory Layer (Short-Term Run State + Long-Term Design Memory)

**Goal:** Give the agent two kinds of memory: short-term (the current run's full state, persisted by the LangGraph checkpointer so a crashed or resumed run continues where it left off) and long-term (a version-controlled record of past design→outcome pairs that informs future runs).

**Why short-term memory is "already solved, but must be wired deliberately":** The LangGraph checkpointer (Phase 11) persists graph state after every super-step — that *is* short-term memory, and hand-rolling it would be redundant. But the *scope* of what goes into state is a real design decision: raw waveforms and VTK fields must never enter graph state (they'd bloat every checkpoint write and every LLM context re-hydration); only the compact structured summaries from Phase 11's tools do. Waveforms live on disk in the run's artifact directory, referenced by path in state.

**Why long-term memory is genuinely suited to this project (not a feature for its own sake):** This agent runs *expensive* simulations (30–90s CHT solves) against *recurring spec classes* (e.g. "48V→12V buck @ 20A, JEDEC board"). A SQLite/JSON store mapping `(spec_signature, chosen_parts, f_sw, geometry_params) → (Tj_max, efficiency, converged, feasible)` means a new run in a familiar spec class starts from the best-known configuration instead of re-searching from scratch, and past *infeasible* specs are recognized without burning simulations proving it again. This is the same reasoning behind Phase 14's fast screen: avoid re-paying for knowledge the system already has. Git-versioned JSON (not a live DB) keeps it inspectable, diffable, and free — consistent with the Phase 1 static-library philosophy.

**Tasks:**
- Checkpointer wiring (SqliteSaver — file-based, no server) with the compact-state discipline above; artifact paths, never blobs, in graph state.
- Long-term design memory: schema for design→outcome records; read path (lookup by spec signature at run start, inject top matches into the reason node's context); write path (append outcome record at run end, including infeasible outcomes).
- Deduplication: identical spec signatures update the existing record rather than appending duplicates.

**Standalone checkpoint:** (1) Kill the orchestrator mid-run, restart on the same thread ID, confirm it resumes from the last completed step with correct state. (2) Run the same spec twice; the second run must show the memory lookup hitting and skipping at least one simulation the first run needed.

---

## Phase 11b — Human-in-the-Loop Approval Gates

**Goal:** Pause the agent for human approval at the decisions where an autonomous wrong turn is most expensive, and resume on command — via LangGraph `interrupt()`, surfaced through a CLI prompt or a small Streamlit approval tab.

**Why gates here and not "before every step":** HITL that interrupts routinely trains the human to approve blindly, which is worse than no gate. The gates belong exactly where cost/irreversibility concentrates: (1) **before the first CHT solve of a new design** — the 30–90s×N-solve optimization loop is the most expensive commit, and a human can sanity-check the chosen parts and heat source before N solves burn; (2) **on infeasible-spec escalation** — when Phase 10's levers are exhausted or the LLM proposes an out-of-policy change (e.g. raising `f_sw`, which *increases* loss, or changing the spec itself), a human decides accept/reject/adjust; (3) **on BOM confirmation** — the final part selection is the deliverable, so a one-time approval before the output bundle is cheap insurance. Routine levers within policy (geometry tweaks, next part down the Rds_on list) must NOT gate — that's what the iteration-count guard in Phase 10 is for.

**Why `interrupt()` rather than a bespoke "wait for input" mechanism:** A bespoke pause means persisting "we are waiting" somewhere and hoping the process stays alive — fragile across crashes and restarts. LangGraph `interrupt()` raises, the checkpointer persists the pending state, and the process is free to die; resume is just invoking the graph again on the same thread with the human's decision in the payload. Approval state survives restarts for free, and the same gate works identically from CLI or a web UI.

**Tasks:**
- Approval-node component: wraps `interrupt()` with a compact decision card (what's proposed, why, key numbers, alternatives considered).
- Gate placement in the graph: pre-CHT, escalation, BOM confirmation.
- Resume surface: CLI prompt first (simplest), then a Streamlit approval tab listing pending gates per run thread with approve/reject/adjust actions.

**Standalone checkpoint:** Run a spec with `Tj` failure forced; verify the run pauses at each gate with a correct decision card, that killing the process at a gate and restarting still shows the pending decision, and that approve/reject/adjust each resume the graph down the correct branch.

---

## Phase 11c — Parallel Design-Space Evaluation

**Goal:** When the mitigation loop needs to compare multiple configurations (e.g. three candidate MOSFETs, or geometry tweaks ×2), evaluate them concurrently rather than sequentially — using LangGraph's `Send` API to fan out, with each branch running its own PySpice→CHT chain.

**Why parallelism pays off *here* specifically:** The dominant cost is the CHT solve (30–90s each), and Phase 10's levers naturally produce *independent* candidate configurations: the three-escalation-lever decision and part-reselection alternatives are mutually independent branches, not a sequence. Evaluating k candidates concurrently turns a k×30–90s serial wait into roughly one solve's duration. foamlib already supports async batch case execution (its documented use case), so the substrate exists — the missing piece is graph-level fan-out/join, which is exactly what `Send` provides.

**Why not Python `asyncio` directly in the tool function:** It would work for the case-runner alone, but the *branches* carry full graph state (candidate params, iteration history) and may individually escalate to HITL gates — a per-tool asyncio pool can't pause one branch at a gate while others continue and then join results. Graph-level fan-out keeps every branch checkpointable, interruptible, and inspectable.

**Design choice — bounded concurrency:** Each CHT branch is CPU/RAM-heavy (mesh + solve on a shared machine). Default concurrency of 2–3 (configurable), not unbounded — unbounded fan-out on a laptop is a swap-death, and the phase's goal is "compare candidates without waiting serially," not "maximize throughput."

**Tasks:**
- Fan-out node: given a set of candidate configurations, dispatch one `Send` per candidate into an isolated evaluation subgraph (Phases 7–9 only — candidates share the same netlist unless the lever is component/frequency change).
- Join node: collect compact summaries, score against the objective (pass spec → lowest loss → lowest cost), feed the winner back into the Phase 10 decision tree.
- Concurrency limiter config; per-branch timeout with a "branch failed" summary that the join treats as worst-scored, not a crash.

**Standalone checkpoint:** Force a scenario needing 3 candidate comparisons; verify branches run concurrently (wall-clock ≪ 3× serial), each branch is independently checkpointed (kill mid-branch, restart, other branches unaffected), and the join selects the correct winner against hand-computed scores.

---

## Phase 12 — Unified Output Bundle

**Goal:** Package each completed run into one inspectable artifact: a JSON manifest plus the `.cir` netlist, BOM CSV, and thermal summary.

**Why bundle instead of scattered files:** Without this, verifying or comparing runs means manually correlating files across several directories. A single manifest per run (spec in, parts chosen, `Tj_max`, efficiency, convergence status, links to the full case/netlist) is the natural handoff both to a human reviewer and to the Phase 13 regression suite.

**Tasks:**
- JSON schema definition for the manifest.
- Bundling script invoked as the last step of a successful (or terminally-failed) agent run.

**Standalone checkpoint:** Validate the manifest against its schema for both a successful and a deliberately-infeasible run.

---

## Phase 13 — Cross-Phase Validation & Regression Suite

**Goal:** A repeatable test suite that runs multiple known reference designs through the *entire* assembled pipeline and flags regressions.

**Why this is separate from Phase 2's own checkpoint:** Phase 2's checkpoint validates the sizing math in isolation. This phase validates that the math still holds up **after** it's been wired through netlist generation, PySpice simulation, loss extraction, and the thermal loop — integration bugs (unit mismatches, sign errors in loss aggregation, mesh scaling errors) typically only show up here, not in any single phase's unit test.

**Tasks:**
- 3–5 reference designs spanning buck/boost/buck-boost, with independently known `L`/`C`/efficiency/`Tj` figures.
- Automated pytest suite, runnable via a single command, ideally wired into free CI (GitHub Actions has a free tier for this scale of project).

**Standalone checkpoint:** The suite itself *is* the checkpoint — green run = no regression.

---

## Phase 14 (Optional) — Tier-1 Foster/Cauer Fast Screen

**Goal:** A sub-0.1-second first-pass thermal check, run inside PySpice itself, to reject obviously-doomed designs before ever invoking the 30–90s OpenFOAM solve.

**Why this is optional relative to your combined 1+2+3 scope, but worth keeping on the roadmap:** You scoped the combined solution to the footprint library + JEDEC template + parametric mesh generator, and that trio is coherent and complete on its own — this phase is additive, not a missing piece of that combination. Its value is purely computational: as your design-space search grows (more specs, more optimization iterations), a near-free screen that filters out hopeless designs before the expensive solve pays for itself. It's also not a novel technique to build from scratch — MOSFET manufacturers (e.g. Nexperia) publish ready-to-use Foster/Cauer RC thermal models as SPICE subcircuits for many parts, meaning this can often be a drop-in addition to the Phase 4 netlist rather than a new model you have to derive.

**Tasks (if/when pursued):**
- Extend the Phase 1 library schema with Foster/Cauer RC parameters where the manufacturer publishes them.
- Add the RC network as a SPICE subcircuit alongside the main netlist, driven by the Phase 5 `P_loss` as a current source.
- Gate the Phase 6–9 OpenFOAM path behind this screen: only proceed to full CHT if the Tier-1 estimate is within a safety margin of the target.

**Key references:**
- Foster/Cauer RC thermal networks as SPICE-compatible manufacturer models, and their standard use for fast `Tj` estimation ahead of full 3D thermal simulation — [Nexperia AN11261, "RC Thermal Models"](https://assets.nexperia.com/documents/application-note/AN11261.pdf).

---

## Summary Table

| Phase | Standalone Test | Key Design Deviation from Original Docs |
|---|---|---|
| 0 | Trivial ngspice + OpenFOAM tutorial runs in container | Pin one OpenFOAM distribution; Docker Engine/Podman over Docker Desktop |
| 1 | Query returns correct filtered parts | Static curated library, not paid distributor API; added Qgd/V_plateau/Ciss |
| 2 | Matches published worked example | Explicit feasibility pre-check as its own step |
| 3 | Netlist parses cleanly | Margin-based part selection, not nearest-value |
| 4 | Detector converges within bounded cycles | Documented upgrade path to shooting-method detection |
| 5 | Matches datasheet efficiency curve | Explicit switching-loss model (was undefined in original) |
| 6 | Correct part placement, no overlap | — |
| 7 | `checkMesh` passes with zero errors | foamlib for scalar edits, Jinja2 only for geometry skeleton |
| 8 | Converges within tutorial's iteration count | **`chtMultiRegionSimpleFoam` instead of `chtMultiRegionFoam`** |
| 9 | Extracted `Tj_max` matches manual ParaView read | Native PyVista reader, no `foamToVTK` step |
| 10 | Escalates through levers correctly on under-cooled case | Entire phase — missing from original pipeline |
| 11 | End-to-end run matches manual phase sequence | **LangGraph state machine (was bare loop)** + ReAct pattern + compact structured returns; CrewAI explicitly rejected |
| 11a | Resume after mid-run kill; second run skips a known sim | Entire phase — new: checkpointer + long-term design memory |
| 11b | Gates pause/resume correctly, survive restarts | Entire phase — new: three cost-justified gates, not gate-per-step |
| 11c | Concurrent candidates, checkpointed branches, correct join | Entire phase — new: `Send` fan-out with bounded concurrency |
| 12 | Manifest validates against schema | Entire phase — new |
| 13 | Suite passes green | Entire phase — new |
| 14 | (n/a — optional) | Reframed as SPICE-subcircuit reuse of manufacturer models, not a from-scratch model |
