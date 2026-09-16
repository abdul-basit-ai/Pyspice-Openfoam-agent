# PySpice–OpenFOAM Agent

AI-assisted DC-DC converter design platform: a deterministic physics and
control layer (sizing, component selection, SPICE verification, loss
extraction, control-loop design, thermal verification) wrapped by an LLM
orchestrator (LangGraph ReAct agent) that only ever *interprets* results —
never computes physics. Optional Streamlit UI.

## Supported topologies

| key          | topology                                   |
|--------------|--------------------------------------------|
| `buck`       | synchronous buck (2 switches)              |
| `boost`      | synchronous boost (2 switches)             |
| `buck_boost` | 4-switch non-inverting buck-boost, +|Vout| |

## Pipeline

```
NL spec parse → memory lookup → sizing (analytical L/C) → component selection
  → screening gate → netlist synthesis + ngspice lint + connectivity check
  → ngspice transient (duty-servoed to the design operating point)
  → per-device loss extraction → Type III control loop w/ delay-aware margins
  → reduced-order electro-thermal convergence
  → (container) OpenFOAM v2406 CHT solve + validation + mitigation loop
  → (optional) NSGA-II Pareto optimization
```

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

Tests:

```bash
.venv/Scripts/python -m pytest tests/        # CHT tests skip off-container
```

Agent + UI:

```bash
streamlit run ui/app.py                      # scripted mode needs no LLM key
# live agent mode: set OPENROUTER_API_KEY in the environment / .env
```

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
   process-wide instance (with `remcirc` between runs) to avoid it.

### OpenFOAM v2406 (CHT thermal tier, Linux/container)

The full conjugate-heat-transfer tier (`run_thermal`, `mitigate_thermal`)
requires `chtMultiRegionSimpleFoam` + `blockMesh` — run inside the provided
Docker image (`docker/`, see `scripts/dtest.sh`). Off-container these tools
return structured errors and the CHT tests skip.

## Layout

- `src/pyspice_openfoam_agent/sizing/` — spec parsing, analytical sizing, screening, topology choice
- `src/pyspice_openfoam_agent/library/` — component database (YAML) + Pydantic schema
- `src/pyspice_openfoam_agent/netlist/` — netlist synthesis, part selection, validation, ngspice lint
- `src/pyspice_openfoam_agent/spice/` — ngspice transient runner, steady-state detector, loss extraction
- `src/pyspice_openfoam_agent/control_loop/` — Type III compensator + delay-aware margin analysis
- `src/pyspice_openfoam_agent/thermal/` — JEDEC board model, OpenFOAM case generation, CHT solver, validation, mitigation, reduced-order electro-thermal loop
- `src/pyspice_openfoam_agent/optimization/` — NSGA-II Pareto (two-tier fidelity)
- `src/pyspice_openfoam_agent/orchestrator/` — LangGraph agent, tool layer, design memory
- `ui/` — Streamlit app; `scripts/` — smoke + dev scripts
