# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An AI-assisted DC-DC power-converter design platform: parse a spec → synthesize topology → select real components → SPICE simulation (PySpice/ngspice) → loss extraction → OpenFOAM conjugate-heat-transfer solve → electro-thermal convergence → NSGA-II Pareto optimization → Streamlit UI, orchestrated by a LangGraph ReAct agent powered by OpenRouter (DeepSeek). Free tools only; deterministic physics everywhere, LLM strictly for reasoning/orchestration.

Two planning docs exist with **different phase numbering**:
- `dc-dc-synthesizer-phase-plan.md` — original build plan (Phases 0–14). The rationale for every design choice (why chtMultiRegionSimpleFoam, why static part library, etc.) lives here.
- `updated_project_goals.md` — the expanded goals renumbering (Phases 0–23) that the code actually implements. Its "Implementation Status" section (at the bottom of the phase-plan doc) tracks what is built.

`PROJECT_STRUCTURE.md` maps directories to phases (kept in sync; trust the code first).

## Commands

```bash
pip install -e .                     # src/ layout install (Python >= 3.10)

python -m pytest tests/              # host run: sim-dependent tests auto-skip
python -m pytest tests/unit/test_sizing.py -k buck    # single test
python -m pytest tests/reference/    # regression suite vs published worked examples

bash scripts/dtest.sh                # full suite inside the docker-agent container
bash scripts/dtest.sh -k sizing      # args forwarded to pytest

docker compose -f docker/docker-compose.yml build    # build canonical env
streamlit run ui/app.py              # UI (inside the container)
```

**Canonical environment is the Docker container** (`docker/Dockerfile`: Ubuntu 22.04, OpenFOAM v2406 openfoam.com line — entrypoint sources its bashrc, ngspice + libngspice symlink, pinned PySpice 1.5 / numpy <2.0). On the Windows host (ngspice installed per README + PySpice DLL layout), everything except the real-CHT `test_solver.py` tests runs natively; those skip unless `chtMultiRegionSimpleFoam` is on PATH (in-container, or source OpenFOAM's bashrc first on Linux).

Expected baseline after the 2026-09 hardening pass (see remain_plan.md DONE log): **210 passed / 6 env-gated skips on the Windows host**; **216/216 in-container** (the 6 host skips are the real-CHT tests, all passing in the container). `scripts/topology_smoke.py` must report ALL PASSED for buck/boost/buck_boost.

LLM access: copy `.env.example` → `.env` and set `OPENROUTER_API_KEY` (`OPENROUTER_MODEL` defaults to `deepseek/deepseek-v4-flash-0731`). Tests use recorded mock LLM responses — the suite never calls the API.

## Architecture

Pipeline modules under `src/pyspice_openfoam_agent/` (each phase is one importable, independently-testable module):

- `sizing/` — NL spec parser (`spec_parser`), feasibility screening, multi-criteria topology selection, analytical L/C sizing (`engine.py`: `Spec` → `size()`).
- `library/` — curated YAML part data (`data/*.yaml`) + Pydantic schema + loader. **The MOSFET catalog is all-Texas-Instruments NexFET** (2026-09-20 rework): every value extracted programmatically from the TI datasheet PDFs, Coss/Crss/Qrr populated on every part; `V_plateau` is a documented conservative estimate (TI does not tabulate it). Inductors are Coilcraft/Würth/Bourns; drivers/controllers are TI/Microchip/onsemi.
- `design/object.py` — the unified **Design object**, central source of truth with `schema_version`; schematic/netlist/BOM/geometry/reports all generate from it and must never silently diverge.
- `netlist/` — margin-based component selection (`selector.py`), `.cir` emission with ngspice lint (`builder.py`), connectivity/floating-node validation (`validate.py`).
- `spice/` — PySpice transient loop with cycle-to-cycle steady-state detection (`steady_state.py`), topology-aware loss extraction (`losses.py`).
- `control_loop/design.py` — Type III compensator; **phase/gain margins computed deterministically by python-control/scipy — never LLM-judged**.
- `thermal/` — JEDEC board geometry (`board.py`), CHT case writer (`case_writer.py` — blockMeshDict is generated PROGRAMMATICALLY from the MeshPlan; there is no Jinja2 template, foamlib is not used), `solver.py` (runs `chtMultiRegionSimpleFoam` with timeout + relaxation fallback), PyVista extraction, result validation, reduced-order thermal tier, `electro_thermal.py` (fixed-point Tj loop: capped 5 iters, |ΔTj| < 2 °C, non-convergence = validation failure).
- `thermal/mitigation.py` — thermal-failure levers in cost order: airflow → component reselection → frequency (exposed via the `mitigate_thermal` tool).
- `optimization/pareto.py` — pymoo NSGA-II over (MOSFET, fsw, L, airflow). **Two-tier fidelity policy**: every candidate uses the reduced-order thermal model; full CHT only for Pareto finalists.
- `orchestrator/` — LangGraph ReAct loop (`graph.py`: reason → act → observe), tool layer (`tools.py`: every capability as a callable over a shared `ToolContext` — sizing/selection/netlist/SPICE/thermal/mitigation/control-loop/electro-thermal plus `run_step_tests` (load/input step transients), `run_sweeps` (load/Vin corners) and `optimize_pareto` (NSGA-II + CHT-verified finalists)), design memory (`memory/design_memory.py`), bounded PROCESS parallelism (`parallel.py` — never threads: one shared ngspice instance per process). Every orchestrated run persists `design.json` (Phase 4 anchor), `manifest.json` (Phase 22 bundle) and `llm_provenance.jsonl` (Phase 18 trail: model, temperature=0, prompt + response per decision call) in the run dir.
- `ui/` — Streamlit app state/renderers used by `ui/app.py` (tabs: Pipeline / Circuit / Thermal / Results; runs the orchestrator in a background thread, polls `RunState` JSON).

### Non-negotiable module contracts

- **Tools return compact dicts** (numbers/status), never raw waveforms/logs/VTK dumps — keeps LLM context and checkpointer state small. Artifacts live on disk under `runs/<run>/`, referenced by path.
- **Tool errors are `{"error": ...}` dicts, not exceptions** — the LLM must observe and recover (ReAct contract).
- The LLM may only choose among parts returned by library queries — it must never invent a part number or rating. Same principle in `control_loop`: LLM interprets computed margins, never computes them.

### Engineering policies (from `updated_project_goals.md`)

- Ask instead of guess on material trade-offs (efficiency vs cost, cooling type, optimization objective); protection settings (OCP/OTP/UVLO) unspecified → ask, and record refusals as unresolved safety items.
- Never silently change the user's spec; infeasible → report the conflict.
- Multi-fidelity order: analytical → screening → SPICE → reduced-order thermal → OpenFOAM CHT.

### Artifacts & memory

- `runs/` — per-run artifacts (gitignored, can be GBs). Fresh `case_<n>/` per iteration, never overwrite.
- `memory_store/design_memory.json` — long-term design→outcome records keyed by spec signature; **committed to Git** (it is the system's memory). Same signature appends an attempt; lookup feeds the best-known config to new runs.

## Conventions

- Physics/validation logic stays deterministic and unit-tested against published worked examples (`tests/reference/regression.py`); LLM behavior is tested with recorded mocks (`tests/unit/test_orchestrator.py`).
- Model + sampling settings sent on every design-decision LLM call (temperature=0) and logged with the full prompt/response to `runs/<run>/llm_provenance.jsonl` (provenance/auditability).
- `.env` is never committed; `build_log.txt` and `docker/phase0_debug.log` are gitignored scratch.
