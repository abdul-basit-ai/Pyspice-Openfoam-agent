# PySpice–OpenFOAM Agent ⚡

**An AI-assisted electronic engineering environment for DC-DC converter design.**
Give it a plain-English spec — *"12 V to 5 V at 3 A, 500 kHz, ripple under 50 mV"* —
and it synthesizes the converter, selects real components, simulates the power
stage in SPICE, designs the feedback compensator, solves the 3D heat flow in
OpenFOAM, and hands you a verified design with an explainable audit trail.

The core principle: **every physics number comes from deterministic,
unit-tested engineering code** (analytical sizing, ngspice, python-control,
OpenFOAM, NSGA-II). The LLM (DeepSeek via OpenRouter, in a LangGraph ReAct
loop) only ever *orchestrates and interprets* — it picks the next tool,
reads the structured result, and explains the trade-offs. It cannot compute
a margin, and it cannot invent a part number: component selection is a
database query, and the schema rejects anything not in the catalog.

![Architecture](diagram.png)

*(The diagram above is rendered from [`docs/architecture.mmd`](docs/architecture.mmd) —
edit that file and re-export to update it.)*

---

## What it does, end to end

| Stage | What happens | Tool |
|---|---|---|
| **Understand** | Parses the NL spec (incl. input ranges like "36–60 V", MHz/Hz units), flags missing safety settings (OCP/OTP/UVLO) as questions, rejects contradictory specs | `size_converter` |
| **Remember** | Looks up past designs with the same signature; the best-known config seeds the new run | `read_design_memory` |
| **Size** | Analytical duty cycle + minimum L/C, two-corner sizing for input ranges (worst of each stress) | `size_converter` |
| **Select** | Real MOSFET / inductor / capacitor (+ gate-driver & controller ICs) from the curated YAML library, with voltage/current/thermal margins; MLCC banking when no single cap meets a tight ripple budget | `select_components` |
| **Screen** | Closed-form feasibility gate — doomed designs are rejected *before* expensive simulation | *inside selection* |
| **Synthesize** | Emits the SPICE netlist (buck / boost / 4-switch non-inverting buck-boost), lints it with ngspice, validates connectivity | `build_netlist` |
| **Verify electrically** | Transient simulation with the duty servo-tracked to the design operating point; measures ripple, efficiency, transient health; extracts per-device losses (conduction, switching, gate, DCR, cap-ESR) | `run_spice` |
| **Verify control** | Type III compensator designed phase-targeted with PWM-delay-aware margins (PM ≥ 45°, GM ≥ 6 dB) computed by python-control — never LLM-judged; load/input step tests measure the plant response | `analyze_control_loop`, `run_step_tests` |
| **Verify thermally** | Reduced-order electro-thermal fixed point (Rds_on tempco) for every design; full 3D conjugate-heat-transfer solve in OpenFOAM v2406 on a JEDEC JESD51-3 board for the operating point, with log-based result validation | `electro_thermal_converge`, `run_thermal` |
| **Recover** | If Tj violates the limit: cost-ordered mitigation (airflow → component reselection → fsw) re-entering the real pipeline, best-effort verdict with honest caveats | `mitigate_thermal` |
| **Explore** | NSGA-II Pareto over (MOSFET, fsw, L, airflow) — reduced-order tier for the search, full CHT verification of the finalists in parallel worker processes | `optimize_pareto` |
| **Deliver** | Every run leaves `manifest.json` (schema-checked bundle), `design.json` (canonical design object) and `llm_provenance.jsonl` (model + temperature + prompt + response of every LLM call) | *finalize* |

Every tool failure or best-effort miss surfaces as a **red caveat banner** in
the UI and a note in the manifest — a summary can never silently bury a
failed check.

## Supported topologies

| key          | topology                                   |
|--------------|--------------------------------------------|
| `buck`       | synchronous buck (2 switches)              |
| `boost`      | synchronous boost (2 switches)             |
| `buck_boost` | 4-switch non-inverting buck-boost, +\|Vout\| |

## Validation status (physics engine vs TI EVM measurements)

The physics engine is validated against real published EVM measurements
(`validation/error_report.md` — full tables, per-field data sourcing, and
reproduction steps):

- **buck — validated**: LM27402 EVM, MAE **0.22 pp** over 17 matched loads
  (worst +0.81 pp; bias slightly optimistic, the dangerous direction).
- **buck-boost — validated with a documented limitation**: LM5175EVM-HD,
  MAE **2.47 pp** over 10 matched loads, uniformly conservative. The
  residual is **not explained** (the real board's 12 V transition point is
  ~98.3% efficient and the modeled gate/crossover terms overshoot loss
  there); it is recorded as an open question with unconfirmed candidate
  causes, not a bounded caveat.
- **boost — NOT validated**: after a genuine TI/ADI/onsemi search, no
  qualifying single-phase discrete-FET synchronous-boost EVM exists with
  schematic + BOM + a digitizable efficiency curve + stated conditions.
  This is an explicit negative result, not a silently-open gap.

Scope: component selection is out of scope (parts injected, not chosen);
one EVM per topology; one operating point / sweep per EVM; **no thermal
validation** on any EVM (ambient/airflow are not stated in the source
docs, so no temperature comparison is claimed).

Future work: a boost EVM if a qualifying one becomes available; closed-loop
SPICE validation (tracked separately in the phase plan); the buck-boost
residual investigation, if picked up later.

## Quickstart

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Windows; use .venv/bin on Linux
```

Run the end-to-end acceptance smoke (one representative spec per topology,
driven through the real orchestrator tool layer — requires `ngspice` on
PATH for the netlist lint and the ngspice shared library for the transient):

```bash
python scripts/topology_smoke.py             # all topologies, no CHT
python scripts/topology_smoke.py --only buck # single topology
python scripts/topology_smoke.py --thermal   # + full CHT (container only)
```

Tests (CHT tests skip automatically off-container):

```bash
.venv/Scripts/python -m pytest tests/
```

Agent + UI:

```bash
streamlit run ui/app.py                      # scripted mode needs no LLM key
# live agent mode: copy .env.example -> .env and set OPENROUTER_API_KEY
```

Current verification baseline: **210 passed / 6 env-gated skips** on the
Windows host; all three topologies pass the smoke end to end. For design-space
coverage there is a full matrix runner — every topology x ripple ratio x
switching frequency through the real tool chain:

```bash
python scripts/matrix_test.py          # 27 cells; --quick for 12
```

Every MOSFET in the library is a **Texas Instruments NexFET** whose values
were extracted programmatically from the TI datasheet PDFs (gate charge,
capacitances, Qrr, thermal resistance) — no aggregator transcriptions, no
invented numbers. The canonical environment (including the CHT tier) is the
Docker image:

```bash
docker compose -f docker/docker-compose.yml build
bash scripts/dtest.sh                        # full suite in-container
```

## Repository layout

- `src/pyspice_openfoam_agent/sizing/` — spec parsing, analytical sizing, screening, advisory topology scorer
- `src/pyspice_openfoam_agent/library/` — component database (MOSFETs, inductors, capacitors, gate drivers, controllers, diodes) + Pydantic schema + queries
- `src/pyspice_openfoam_agent/design/` — the canonical, schema-versioned Design object
- `src/pyspice_openfoam_agent/netlist/` — netlist synthesis, margin-based selection, validation, ngspice lint
- `src/pyspice_openfoam_agent/spice/` — ngspice transient runner, steady-state detector, loss extraction, step tests, sweeps
- `src/pyspice_openfoam_agent/control_loop/` — Type III compensator + delay-aware margin analysis
- `src/pyspice_openfoam_agent/thermal/` — JEDEC board model, OpenFOAM case generation, CHT solver, validation, mitigation, electro-thermal loop
- `src/pyspice_openfoam_agent/optimization/` — NSGA-II Pareto (two-tier fidelity)
- `src/pyspice_openfoam_agent/orchestrator/` — LangGraph agent, tool layer, design memory, bounded process parallelism
- `ui/` — Streamlit app; `scripts/` — smoke + dev scripts; `report/` — IEEE-format project report (LaTeX); `docs/` — diagram source

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — agent-facing guide: architecture, contracts, engineering policies
- [`updated_project_goals.md`](updated_project_goals.md) — the phase plan (0–23) with implementation status notes
- [`AUDIT.md`](AUDIT.md) — the full audit: every finding → fix → verification
- [`PROJECT_STRUCTURE.md`](PROJECT_STRUCTURE.md) — directory → phase map
- [`report/dc_dc_agent_report.tex`](report/dc_dc_agent_report.tex) — 4–6 page IEEE-format report of the whole project (architecture, engineering decisions, audit, EVM validation); compile with pdflatex or import into Overleaf (packages: amsmath, graphicx, booktabs, tikz, pgfplots, siunitx, multirow, hyperref — all in TeX Live default)

## Environment notes

### ngspice on Windows (SPICE stages)

1. Install ngspice (console build) and add its `bin/` to PATH — used by the
   `ngspice -b` netlist lint.
2. PySpice loads the ngspice **shared DLL** from
   `.venv/Lib/site-packages/PySpice/Spice/NgSpice/Spice64_dll/dll-vs/`.
   Copy `ngspice.dll` (rename to `ngspice0.dll` is optional), plus its
   dependencies `sndfile.dll`, `samplerate.dll`, `libomp140.x86_64.dll`,
   and the `share/ngspice` + `lib/ngspice` trees from the official Windows
   package into that layout. `spice/runner.py` adds the DLL directory to the
   process PATH automatically before the first load.
3. PySpice 1.5 crashes with `cffi.CDefError` if a SECOND NgSpiceShared
   instance is created in one process — `spice/runner.py` uses a single
   process-wide instance (with `remcirc` between runs) to avoid it, and
   candidate parallelism therefore uses **processes, not threads**.

### OpenFOAM v2406 (CHT thermal tier, Linux/container)

The full conjugate-heat-transfer tier (`run_thermal`, `mitigate_thermal`,
Pareto finalist verification) requires `chtMultiRegionSimpleFoam` +
`blockMesh` — run inside the provided Docker image (`docker/`, see
`scripts/dtest.sh`). Off-container these tools return structured errors and
the CHT tests skip.
