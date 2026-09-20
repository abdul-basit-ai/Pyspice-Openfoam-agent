# Project layout — Pyspice-Openfoam-agent

Maps each directory to its phase(s) in dc-dc-synthesizer-phase-plan.md.
(Refreshed after the 2026-09 audit; trust the code first, this is the map.)

```
Pyspice-Openfoam-agent/
├── dc-dc-synthesizer-phase-plan.md   # build plan (Phases 0-14 + 11a/11b/11c)
├── README.md                         # quickstart + environment setup
├── AUDIT.md                          # 2026-09 full audit: findings -> fixes -> verification
├── CLAUDE.md                         # agent-facing guide
├── .gitignore
├── .env.example                      # LLM API keys (copy to .env, never commit .env)
│
├── docker/
│   ├── Dockerfile                    # Phase 0: pinned OpenFOAM v2406 + ngspice + Python
│   └── docker-compose.yml            # Phase 0: reproducible build/run
│
├── src/pyspice_openfoam_agent/
│   ├── __init__.py
│   ├── config.py                     # shared settings (paths, solver binaries)
│   ├── library/                      # Phase 1: component library + schema
│   │   ├── schema.py                 #   Pydantic models (sanity ranges on datasheet fields)
│   │   ├── loader.py                 #   load + query (filter by rating)
│   │   └── data/                     #   mosfets/inductors/capacitors + gate_drivers/
│   │   │                             #   controllers/diodes (IC categories, Group A1)
│   ├── sizing/                       # Phase 2: feasibility, topology, sizing
│   │   ├── spec_parser.py            #   NL spec -> Requirements (regex + ask policy)
│   │   ├── engine.py                 #   Spec/feasibility/classify/analytical L,C sizing
│   │   ├── screening.py              #   Phase 7: fast closed-form design gate
│   │   └── topology_select.py        #   multi-criteria topology scorer (advisory)
│   ├── design/object.py              # unified Design object (schema-versioned)
│   ├── netlist/                      # Phases 3+5: part selection + .cir emission
│   │   ├── selector.py               #   margin-based selection (L/Isat/Irms/Vds/Id)
│   │   ├── builder.py                #   buck / boost / 4-switch buck_boost synthesis,
│   │   │                             #     dead time, duty-trim support, ngspice lint
│   │   └── validate.py               #   static connectivity check
│   ├── spice/                        # Phases 4-5: ngspice runs + loss extraction
│   │   ├── runner.py                 #   raw-ngspice .tran wrapper (ONE shared instance:
│   │   │                             #     PySpice parser drops S-elements; PySpice 1.5
│   │   │                             #     crashes on a second instance per process)
│   │   ├── steady_state.py           #   cycle-to-cycle detector (sustained-flatness +
│   │   │                             #     expected-level guards)
│   │   ├── losses.py                 #   topology-aware per-device loss extraction
│   │   ├── step_tests.py             #   Phase 6/8: load/input step injection + response
│   │   │                             #     measurement (open-loop plant; Group A3)
│   │   └── sweeps.py                 #   Phase 8: load sweep (Rload surgery) on the
│   │                                 #     fixed servo'd design (Group A4)
│   ├── control_loop/design.py        # Phase 6: Type III compensator, delay-aware margins
│   ├── thermal/                      # Phases 6-10: board, mesh, CHT solve, extraction
│   │   ├── board.py                  #   JEDEC JESD51-3 geometry (114.3 x 76.2 mm)
│   │   ├── mesh_generator.py         #   direct blockMeshDict generation (no Jinja2)
│   │   ├── case_writer.py            #   OpenFOAM case assembly (cpuCabinet scaffold)
│   │   ├── solver.py                 #   Phase 8: chtMultiRegionSimpleFoam + relaxation
│   │   │                             #     ladder, per-attempt logs, endTime check
│   │   ├── extraction.py             #   Phase 9: PyVista Tj extraction
│   │   ├── validation.py             #   Phase 12: log-based result validation
│   │   ├── electro_thermal.py        #   Phase 13: damped fixed-point Tj loop
│   │   └── mitigation.py             #   Phase 10: airflow/reselect/fsw decision tree
│   ├── optimization/pareto.py        # Phase 13: NSGA-II + reduced-order tier-1 model
│   ├── orchestrator/                 # Phases 11+: LangGraph agent
│   │   ├── graph.py                  #   state graph, ReAct nodes, finalize recording
│   │   ├── tools.py                  #   9 tools (incl. mitigate_thermal + run_spice
│   │   │                             #     duty servo), ReAct error contract
│   │   ├── parallel.py               #   Phase 15: bounded PROCESS pool for independent
│   │   │                             #     candidate pipelines (threads unsafe: one shared
│   │   │                             #     ngspice instance per process)
│   │   └── memory/
│   │       └── design_memory.py      #   Phase 11a: git-versioned store (atomic writes)
│   ├── bundle/manifest.py            # Phase 12: output manifest
│   └── ui/                           # run_state bridge, schemdraw schematics, PyVista viewer
│
├── runs/                             # per-run artifacts (gitignored): case_<n>/, waveforms/
├── memory_store/                     # Phase 11a: Git-versioned design->outcome records
│
├── tests/
│   ├── unit/                         # per-module tests (mirrors src layout)
│   └── reference/                    # Phases 2/13: published worked examples, regression specs
│
├── scripts/
│   ├── topology_smoke.py             # end-to-end acceptance smoke (one spec per topology)
│   └── dtest.sh                      # in-container pytest runner
│
└── ui/app.py                         # Streamlit front end (scripted + OpenRouter modes)
```

Conventions:
- All Python under `src/` layout (installable via `pip install -e .`).
- Sim artifacts (runs/) never enter Git; memory_store/ does (it is the long-term memory).
- Tests carry the phase checkpoints — each phase's "standalone checkpoint" is a test file here.
- The orchestrator `hitl/` sub-package from the original plan was never built and has been
  removed (Phase 20 is formally descoped in updated_project_goals.md). Phase 15 parallelism
  lives in `orchestrator/parallel.py` (a module, not the old stub package).
