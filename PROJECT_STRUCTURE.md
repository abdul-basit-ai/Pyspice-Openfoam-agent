# Project layout — Pyspice-Openfoam-agent

Maps each directory to its phase(s) in dc-dc-synthesizer-phase-plan.md.

```
Pyspice-Openfoam-agent/
├── dc-dc-synthesizer-phase-plan.md   # build plan (Phases 0-14 + 11a/11b/11c)
├── README.md
├── .gitignore
├── .env.example                      # LLM API keys (copy to .env, never commit .env)
│
├── docker/
│   ├── Dockerfile                    # Phase 0: pinned OpenFOAM + ngspice + Python
│   └── docker-compose.yml            # Phase 0: reproducible build/run
│
├── src/pyspice_openfoam_agent/
│   ├── __init__.py
│   ├── library/                      # Phase 1: component library + schema
│   │   ├── schema.py                 #   YAML/JSON schema models
│   │   ├── loader.py                 #   load + query (filter by rating)
│   │   └── data/                     #   mosfets.yaml, inductors.yaml, capacitors.yaml
│   ├── sizing/                       # Phase 2: feasibility, topology, sizing
│   │   ├── precheck.py
│   │   ├── topology.py
│   │   └── sizing_engine.py
│   ├── netlist/                      # Phase 3: part selection + .cir emission
│   │   ├── selector.py
│   │   └── builder.py
│   ├── spice/                        # Phases 4-5: PySpice runs + loss extraction
│   │   ├── runner.py                 #   .tran wrapper, waveform arrays
│   │   ├── steady_state.py           #   cycle-to-cycle detector (+ shooting-method slot)
│   │   └── losses.py                 #   conduction + switching loss models
│   ├── thermal/                      # Phases 6-9: board, mesh, CHT solve, extraction
│   │   ├── board_template.py         #   Phase 6: JEDEC geometry data structure
│   │   ├── mesh_generator.py         #   Phase 7: Jinja2 blockMeshDict
│   │   ├── case_editor.py            #   Phase 7: foamlib scalar edits
│   │   ├── solver.py                 #   Phase 8: CHT automation + safeguards
│   │   └── extraction.py             #   Phase 9: PyVista Tj extraction
│   ├── mitigation/                   # Phase 10: decision tree + guards
│   │   └── levers.py
│   ├── orchestrator/                 # Phases 11-11c: LangGraph agent
│   │   ├── graph.py                  #   state graph, ReAct nodes
│   │   ├── tools.py                  #   tool schemas wrapping the modules above
│   │   ├── state.py                  #   compact state schema (paths, not blobs)
│   │   ├── memory/                   #   Phase 11a: checkpointer wiring + design store
│   │   │   ├── checkpointer.py
│   │   │   └── design_memory.py
│   │   ├── hitl/                     #   Phase 11b: interrupt() gates + resume surfaces
│   │   │   ├── gates.py
│   │   │   └── approval_cli.py
│   │   └── parallel/                 #   Phase 11c: Send fan-out + join
│   │       └── fanout.py
│   ├── bundle/                       # Phase 12: output manifest
│   │   └── manifest.py
│   └── config.py                     # shared settings (paths, concurrency limits)
│
├── templates/                        # Phase 7: Jinja2 OpenFOAM case skeletons
│   └── blockMeshDict.j2
│
├── runs/                             # per-run artifacts (gitignored): case_<n>/, waveforms/
├── memory_store/                     # Phase 11a: Git-versioned design→outcome records
│
├── tests/
│   ├── unit/                         # per-module tests (mirrors src layout)
│   └── reference/                    # Phases 2/13: published worked examples, regression specs
│
├── scripts/
│   └── run_agent.py                  # CLI entry: spec -> orchestrated run
│
└── ui/                               # Phase 11b: Streamlit approval tab (later)
```

Conventions:
- All Python under `src/` layout (installable via `pip install -e .`).
- Sim artifacts (runs/) never enter Git; memory_store/ does (it is the long-term memory).
- Tests carry the phase checkpoints — each phase's "standalone checkpoint" is a test file here.
