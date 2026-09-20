i # Advanced AI Power Electronics Design & Multiphysics Platform

## 1. Project Goal

Build an AI-assisted engineering platform that can take a
power-converter specification, synthesize a complete electrical design,
represent it as an editable circuit, simulate its electrical behavior,
estimate losses, construct a physically meaningful thermal model, run
high-fidelity OpenFOAM thermal/CFD simulations, and iteratively optimize
the design.

The project should evolve from a linear simulation pipeline into a
**closed-loop engineering design environment**.

The target workflow is:

``` text
User specification
      ↓
Requirements + constraints
      ↓
Design decisions
      ↓
Topology synthesis
      ↓
Component selection
      ↓
Circuit representation / schematic
      ↓
Analytical screening
      ↓
SPICE electrical simulation
      ↓
Loss extraction
      ↓
PCB / physical geometry
      ↓
OpenFOAM thermal simulation
      ↓
Validation
      ↓
Multi-objective optimization
      ↓
Best feasible designs
      ↓
Human review
      ↓
Final engineering package
```

The system should not blindly make engineering assumptions. It must
distinguish between:

1.  **Required design constraints** supplied by the user.
2.  **Established engineering design rules** that can safely be applied
    automatically.
3.  **Design choices with meaningful trade-offs** that should either be
    explicitly configured by policy or presented to the user for
    approval.

When a decision materially affects cost, safety, topology, performance,
manufacturability, or the interpretation of the specification, the agent
should **ask the user rather than silently choosing**.

------------------------------------------------------------------------

# 2. Core Design Principles

These principles apply to every phase.

### 2.1 Good engineering choices by default

The system should follow established engineering practices wherever the
choice is unambiguous.

Examples:

-   adequate voltage and current derating
-   realistic component tolerances
-   thermal margins
-   stable simulation settings
-   physically plausible component models
-   conservative numerical convergence settings
-   reproducible simulations
-   traceable assumptions
-   validation against references
-   no silently invalid component substitutions

The agent should prefer a defensible engineering choice over the
mathematically smallest or cheapest choice.

### 2.2 Ask instead of guessing

If multiple valid engineering choices exist and the correct choice
depends on user priorities, the agent should ask.

Examples:

-   efficiency vs. cost
-   switching frequency vs. thermal performance
-   component size vs. performance
-   natural convection vs. forced airflow
-   conservative vs. aggressive thermal limits
-   preferred topology where several are viable
-   component availability
-   PCB size
-   acceptable ripple
-   target efficiency
-   optimization objective

The agent should provide a **provisional recommendation** when possible,
but clearly state the assumption and offer the user a chance to change
it.

### 2.3 Explain important decisions

Every major design decision should have:

``` text
Decision
Reason
Constraints considered
Alternatives considered
Assumptions
Confidence / validation status
```

The system should be able to answer:

> Why did you choose this topology?

> Why this MOSFET?

> Why this switching frequency?

> Why this inductance?

> Why did you change the design?

### 2.4 Never silently change the specification

The agent may optimize implementation parameters, but it must not
silently change:

-   Vin/Vout requirements
-   current requirements
-   ripple limits
-   efficiency targets
-   thermal limits
-   safety constraints
-   PCB constraints
-   user-defined component restrictions

If the specification appears impossible, the agent should report the
conflict and ask whether the user wants to relax a constraint.

### 2.5 Multi-fidelity simulation

Use the cheapest sufficiently accurate model first.

``` text
Analytical
   ↓
Fast screening
   ↓
SPICE
   ↓
Reduced-order thermal model
   ↓
OpenFOAM
   ↓
Experimental validation
```

Do not spend expensive CFD simulation time on designs that can already
be rejected analytically.

------------------------------------------------------------------------

# 3. Phase Dependency Map

``` text
Phase 0  Environment & Reproducibility
   ↓
Phase 1  Component + Model Database
   ↓
Phase 2  Requirements & Feasibility
   ↓
Phase 3  Topology Synthesis + Analytical Sizing
   ↓
Phase 4  Unified Design Representation
   ↓
Phase 5  Component Selection + Circuit/Schematic Generation
   ↓
Phase 6  Control-Loop Design & Validation
   ↓
Phase 7  Fast Electrical Screening
   ↓
Phase 8  SPICE Simulation + Steady-State Detection
   ↓
Phase 9  Loss Extraction + Electrical Validation
   ↓
Phase 10  PCB / Physical Geometry Generation
   ↓
Phase 11 Parametric Mesh + OpenFOAM Case Generation
   ↓
Phase 12 CHT Thermal Simulation
   ↓
Phase 13 Thermal Result Extraction + Validation
   │
   │  electro-thermal loop: recompute losses at the new Tj,
   │  re-run Phase 9 → Phase 13 until ΔTj < tolerance
   └──→ back to Phase 9
   ↓
Phase 14 Multi-Objective Optimization
   ↓
Phase 15 Design-Space Exploration + Parallel Evaluation
   ↓
Phase 16 Engineering Decision / Feedback Loop
   ↓
Phase 17 Interactive Schematic + Simulation UI
   ↓
Phase 18 AI Engineering Orchestrator
   ↓
Phase 19 Memory + Design Knowledge Base
   ↓
Phase 20 HITL Approval Gates
   ↓
Phase 21 Experimental / Hardware Validation
   ↓
Phase 22 Unified Engineering Output
   ↓
Phase 23 Cross-Phase Regression Suite
```

------------------------------------------------------------------------

# Phase 0 --- Environment & Reproducibility

## Goal

Create a reproducible environment containing the electrical, thermal,
optimization, and AI tooling.

## Engineering policy

The environment should use pinned versions and deterministic
configuration wherever practical.

The agent should not silently switch between incompatible OpenFOAM
distributions or simulation versions.

If the environment has multiple technically valid choices, such as
OpenFOAM distributions, the system should either use the project-defined
default or ask the user during initial setup.

## Tasks

-   Pin Python dependencies.
-   Pin ngspice.
-   Pin OpenFOAM distribution/version.
-   Configure PySpice.
-   Configure LangGraph.
-   Configure foamlib.
-   Configure PyVista.
-   Configure Jinja2.
-   Configure python-control (loop-gain/margin analysis, MIT-style license).
-   Configure pymoo (multi-objective optimization, NSGA-II/III, free).
-   Pin the orchestrator LLM model version and log temperature=0 (or the
    chosen sampling settings) for every design-decision call.
-   Create reproducible build/run scripts.
-   Add automated environment validation.

## Checkpoint

Run:

-   a basic ngspice circuit
-   a PySpice simulation
-   an OpenFOAM tutorial
-   a minimal CHT case

with no manual intervention.

------------------------------------------------------------------------

# Phase 1 --- Component & Physics Model Database

## Goal

Create a version-controlled library containing real electrical
components and the models required for electrical and thermal
simulation.

## Data

MOSFETs should include, where available:

-   Vds
-   Id
-   Rds_on
-   Rds_on temperature coefficient (required for the electro-thermal
    convergence loop in Phase 9)
-   Qg
-   Qgd
-   Vplateau
-   Ciss
-   Coss
-   Crss
-   package
-   dimensions
-   thermal resistance
-   thermal model
-   manufacturer
-   datasheet source
-   operating conditions for published parameters

Inductors should include:

-   inductance
-   DCR
-   DCR temperature coefficient
-   core-loss model or parameters (required for electro-thermal convergence)
-   saturation current
-   RMS current
-   dimensions
-   thermal information

Capacitors should include:

-   capacitance
-   voltage rating
-   ESR
-   ESR temperature coefficient
-   ripple-current rating
-   tolerance
-   temperature characteristics

Gate driver ICs should include, where available:

-   part number
-   drive voltage range
-   peak source/sink current
-   propagation delay
-   package
-   thermal data
-   manufacturer
-   datasheet source

Controller ICs should include, where available:

-   part number
-   control law (voltage mode, peak current mode, etc.)
-   switching-frequency range
-   reference voltage and accuracy
-   error-amplifier characteristics
-   package
-   thermal data
-   manufacturer
-   datasheet source

Diodes/synchronous rectifiers should include, where available:

-   Vf (forward voltage) and its temperature coefficient
-   reverse recovery characteristics
-   current rating
-   package
-   thermal data

## Design-choice policy

Component selection must include electrical and thermal margins.

The agent should not select a part solely because it has the lowest
nominal Rds_on.

If the best component depends on an unclear trade-off, such as:

> lower Rds_on vs. higher Qg

the agent should explain the trade-off and either use the configured
optimization policy or ask the user.

## Checkpoint

Query the database and verify component filtering against hand-checked
datasheets.

------------------------------------------------------------------------

# Phase 2 --- Requirements, Constraints & Feasibility

## Goal

Convert the user's natural-language specification into a structured
engineering specification.

Example:

``` text
Vin = 36–60 V
Vout = 12 V
Iout = 20 A
Ripple < 100 mV
Efficiency > 95%
Tj < 100 °C
```

## Tasks

-   Parse requirements.
-   Identify missing parameters.
-   Validate units.
-   Detect contradictory constraints.
-   Detect impossible specifications.
-   Identify design variables.
-   Identify hard constraints vs. optimization objectives.

## User-question policy

The agent should ask when a missing requirement materially changes the
design.

Protection requirements (OCP, OTP, UVLO) are safety-relevant: if the user
does not specify them, the agent must ask for them rather than assuming
values. If the user declines to specify, record the omission explicitly in
the design object as an unresolved safety item.

For example:

> "You specified 48 V → 12 V at 20 A, but not the allowed output ripple.
> Should I assume 1% of Vout, use a typical power-converter target, or
> let you specify it?"

For non-critical defaults, the agent may use a documented engineering
assumption.

## Checkpoint

Produce a complete structured specification from several
natural-language examples and correctly identify missing/contradictory
constraints.

------------------------------------------------------------------------

# Phase 3 --- Topology Synthesis & Analytical Sizing

## Goal

Select a feasible topology and produce an initial design using
analytical models.

Possible topologies include:

-   buck
-   boost
-   buck-boost
-   synchronous buck
-   other supported converter families

## Tasks

-   Topology feasibility.
-   Duty-cycle calculation.
-   Inductor sizing.
-   Capacitor sizing.
-   Ripple estimation.
-   Current stress estimation.
-   Voltage stress estimation.
-   Initial switching-frequency selection.

## Design-choice policy

The topology should not be selected only from a voltage ratio.

The agent should consider:

-   efficiency
-   current
-   voltage stress
-   component count
-   switching losses
-   control complexity
-   thermal implications
-   user constraints

If multiple topologies are reasonable, the agent should present the
candidates and ask the user to choose unless an optimization policy
already exists.

## Checkpoint

Compare sizing results against published worked examples.

------------------------------------------------------------------------

# Phase 4 --- Unified Design Representation

## Goal

Create a single internal representation of the design.

The design object becomes the central source of truth.

``` text
Design
 ├── requirements
 ├── topology
 ├── parameters
 ├── components
 ├── connections
 ├── constraints
 ├── optimization objectives
 ├── schematic
 ├── electrical model
 ├── physical model
 └── simulation configuration
```

Everything else should be generated from this representation:

``` text
Design
 ├── schematic
 ├── SPICE netlist
 ├── BOM
 ├── PCB geometry
 ├── OpenFOAM case
 └── reports
```

## Schema versioning

The Design object carries a `schema_version` field. When the schema
changes in later phases, old serialized designs stored in Phase 19's
memory must either be migrated to the current version or explicitly
marked as loaded under a prior schema version — never silently
reinterpreted under the new schema.

## Design-choice policy

Do not allow separate representations to silently diverge.

If the schematic changes, the SPICE netlist and dependent artifacts must
be regenerated.

## Checkpoint

Create a design object, serialize it, reload it, and regenerate the same
netlist and metadata.

------------------------------------------------------------------------

# Phase 5 --- Component Selection + Circuit/Schematic Generation

## Goal

Convert analytical requirements into an actual circuit using real
components.

## Tasks

-   Select components, including gate driver and controller ICs from the
    Phase 1 database.
-   Apply voltage/current/thermal margins.
-   Generate connections.
-   Generate SPICE netlist.
-   Generate graphical schematic.
-   Validate connectivity.
-   Detect floating nodes.
-   Detect invalid connections.
-   Check component ratings.

## User-question policy

If component selection depends on an unresolved preference, ask.

Example:

> "MOSFET A is cheaper, while MOSFET B gives approximately 1% better
> predicted efficiency and lower temperature. Which should the optimizer
> prioritize?"

If the user has already specified:

``` text
objective = maximum efficiency
```

the agent should make the decision automatically.

## Checkpoint

Generate a valid schematic and SPICE netlist from the same design
representation.

------------------------------------------------------------------------

# Phase 6 --- Control-Loop Design & Validation

## Goal

Design and validate the feedback compensator so the converter is a
controlled, stable system — not just a power stage.

## Tasks

-   Select the controller IC / control law (voltage mode, peak current
    mode, etc.) from the Phase 1 controller-IC database.
-   Select gate driver ICs from the Phase 1 gate-driver database.
-   Design the compensator (Type II or Type III) for the chosen control
    law and power-stage characteristics.
-   Compute loop gain (magnitude and phase) on the averaged small-signal
    model.
-   Verify phase margin >= 45 degrees and gain margin >= 6 dB.
-   Check transient response (overshoot, settling time) on the load-step
    and input-step SPICE tests from Phase 8.

## Tooling decision

Use the `python-control` library (free, MIT-style license) for Bode and
margin analysis on the averaged small-signal model. Stability margins are
computed deterministically by the tool — the LLM must never judge
stability margins itself; it may only interpret the computed results.

## Design-choice policy

Compensator type (Type II vs. Type III) and controller-IC selection
involve trade-offs (component count, bandwidth, cost). If the choice is
not covered by an optimization policy, present the candidates and ask.

## Checkpoint

Produce a compensator design with computed phase/gain margins meeting the
limits above, plus load-step and input-step SPICE runs showing acceptable
transient response.

------------------------------------------------------------------------

# Phase 7 --- Fast Electrical Screening

## Goal

Reject obviously poor designs before expensive simulations.

## Tasks

Perform fast calculations for:

-   voltage stress
-   current stress
-   conduction loss
-   approximate switching loss
-   estimated efficiency
-   ripple
-   component thermal stress

## Policy

Any design that clearly violates a hard constraint should be rejected
before SPICE.

If a design is borderline, continue to higher-fidelity simulation rather
than rejecting it solely on a rough model.

------------------------------------------------------------------------

# Phase 8 --- SPICE Simulation & Steady-State Detection

## Goal

Perform detailed electrical simulation using PySpice/ngspice.

## Tasks

-   transient simulation
-   startup analysis
-   steady-state detection
-   output ripple
-   load current
-   switch voltage/current
-   efficiency
-   transient response

## Advanced capability

Support multiple operating conditions:

``` text
Vin sweep
Load sweep
Temperature sweep
Component tolerance sweep
Startup
Load step
Input step
```

> **STATUS (2026-09-20):** Vin sweep, load sweep, startup, load step and
> input step are implemented (`run_sweeps`, `run_step_tests` tools).
> Temperature and component-tolerance sweeps are DESCOPED: the Phase 3 rig
> carries no temperature-dependent component models or tolerance sampling —
> stated rather than half-implemented.

## User-question policy

If the requested simulation envelope is unclear, use a conservative
default only when appropriate and tell the user what was assumed.

For expensive sweeps, ask for the desired scope if it materially affects
runtime.

------------------------------------------------------------------------

# Phase 9 --- Loss Extraction & Electrical Validation

## Goal

Convert simulation waveforms into physically meaningful losses.

Calculate:

-   MOSFET conduction loss
-   MOSFET switching loss
-   diode loss
-   inductor DCR loss
-   capacitor ESR loss
-   total converter loss

Keep losses localized by component.

## Validation

Compare results against:

-   analytical calculations
-   datasheet curves
-   published reference designs
-   known test cases

Large discrepancies must trigger investigation rather than being
silently accepted.

## Electro-thermal convergence

Component losses depend on junction temperature, and junction temperature
depends on losses. Resolve this with a fixed-point iteration:

``` text
losses (at assumed Tj) → thermal solve → new Tj → recompute losses → ...
```

Iterate until the change in Tj between iterations falls below a stated
tolerance (e.g. 2 °C), capped at N iterations (e.g. 5). Each iteration
recomputes Rds_on, Vf, and core losses at the newly found Tj using the
temperature coefficients stored in the Phase 1 database.

If the loop fails to converge within the cap, treat it as a validation
failure under the existing discrepancy policy — investigate, and ask the
user if the cause is an ambiguous modeling assumption.

## User-question policy

If the discrepancy may result from an ambiguous component model or
operating condition, report the ambiguity and ask for the preferred
modeling assumption when necessary.

------------------------------------------------------------------------

# Phase 10 --- PCB & Physical Geometry

## Goal

Translate the electrical design into a physically meaningful
board/component representation.

## Tasks

-   board geometry
-   copper layers
-   component footprints
-   package dimensions
-   component placement
-   heat-spreading regions
-   thermal interfaces
-   airflow domain

## Advanced capability

Allow physical placement to become an optimization variable.

> **STATUS (2026-09-20):** DESCOPED. The board template is deliberately
> FIXED (JEDEC JESD51-3 deterministic layout) — placement-as-variable would
> break the datasheet comparability that fixed geometry provides. Revisit
> only if a use case demands a custom-layout mode SEPARATE from the JEDEC
> reference path.

Optimize:

-   component spacing
-   copper area
-   thermal paths
-   airflow exposure
-   hotspot separation

## User-question policy

If the PCB dimensions, layer stackup, copper weight, or cooling method
are not specified and materially affect thermal results, ask the user.

Do not silently invent a manufacturing constraint and present it as a
user requirement.

------------------------------------------------------------------------

# Phase 11 --- Parametric Mesh & OpenFOAM Case Generation

## Goal

Generate a valid CFD/CHT case from the physical design.

## Tasks

-   geometry generation
-   mesh generation
-   boundary conditions
-   material properties
-   heat-source placement
-   airflow parameters
-   solver configuration

Use structured, robust geometry generation wherever practical.

## Design-choice policy

Mesh density should be selected based on the physics and required
accuracy, not arbitrarily.

If a higher-fidelity mesh would significantly increase runtime, expose
the trade-off.

The user should be able to choose:

``` text
Fast
Balanced
High fidelity
```

unless the project has a predefined policy.

------------------------------------------------------------------------

# Phase 12 --- OpenFOAM CHT Thermal Simulation

## Goal

Calculate the thermal behavior of the physical implementation.

Inputs include:

-   component losses
-   board geometry
-   package geometry
-   material properties
-   airflow
-   boundary conditions

Outputs include:

-   temperature field
-   Tj_max
-   Tj_avg
-   board temperature
-   hotspot locations
-   airflow information
-   convergence status

## Checkpoint

Validate against known tutorial/reference cases and verify mesh and
solver convergence.

## Scope decision

OpenFOAM CHT simulations in this project are steady-state only, sized
around the worst-case sustained load. Transient thermal response to
load/input steps is explicitly out of scope and future work. No later
phase may assume transient thermal data exists; transient electrical
behavior (from SPICE) is the substitute where needed.

## User-question policy

If cooling conditions are unspecified, do not assume forced airflow
without confirmation.

Ask:

> "Should I model natural convection, a specified airflow velocity, or
> an actual fan/cooling system?"

------------------------------------------------------------------------

# Phase 13 --- Thermal Result Extraction & Validation

## Goal

Extract thermal metrics automatically and verify that the result is
trustworthy.

Check:

-   convergence
-   residual behavior
-   energy balance
-   maximum temperature
-   temperature margins
-   mesh quality
-   sensitivity to solver settings

A temperature number without convergence evidence should not be treated
as a validated result.

## Electro-thermal loop-back

On a validated result, feed the new Tj back to Phase 9: recompute
Rds_on, Vf, and core losses at the new temperature and re-run the loss →
thermal loop until ΔTj between iterations is below tolerance (see the
Electro-thermal convergence subsection in Phase 9). Non-convergence is a
validation failure, not a result.

------------------------------------------------------------------------

# Phase 14 --- Multi-Objective Optimization

## Goal

Move from pass/fail optimization to real engineering design-space
exploration.

Optimize multiple objectives such as:

-   efficiency
-   Tj_max
-   cost
-   PCB area
-   component count
-   ripple
-   transient performance
-   thermal margin

Generate a Pareto frontier when objectives conflict.

Example:

``` text
Efficiency ↑

98% |             ●
97% |        ●         ●
96% |    ●
95% | ●
    +------------------------→ Cost
```

## User-question policy

The agent must ask when the optimization priorities are unclear.

Example:

> "Should I prioritize maximum efficiency, minimum cost, minimum
> temperature, or a balanced Pareto solution?"

Never assume the user's objective from the specification alone when
multiple interpretations are plausible.

## Two-tier thermal fidelity policy

Full 3D CHT simulation per candidate inside an outer optimization loop is
computationally infeasible on local, non-cloud, free-tier hardware.
Therefore:

``` text
Design-space search (Phase 15):
    every candidate evaluated with a fast reduced-order thermal model
    (RC thermal network or analytical spreading-resistance model)
Pareto-front finalists (top 3-5):
    verified with full OpenFOAM CHT (Phase 12) before results are
    presented to the user
```

Reduced-order models must be validated against the CHT results of past
runs (Phase 19 memory) so the fast/fidelity gap stays quantified.

------------------------------------------------------------------------

# Phase 15 --- Design-Space Exploration & Parallel Evaluation

## Goal

Evaluate multiple promising candidates concurrently.

Candidate dimensions may include:

-   MOSFET
-   inductor
-   capacitor
-   switching frequency
-   topology
-   cooling
-   PCB placement
-   geometry

Use bounded concurrency to avoid exhausting system resources.

Each candidate must have isolated artifacts and reproducible parameters.

Candidate evaluation during exploration uses the fast reduced-order
thermal model per the two-tier thermal fidelity policy in Phase 14; full
CHT is reserved for Pareto-front finalists.

> **IMPLEMENTATION STATUS (2026-09-20):** Implemented as bounded PROCESS
> parallelism (`orchestrator/parallel.py` + `optimize_pareto.max_workers`).
> Processes, not threads, are mandatory here: `spice/runner.py` keeps one
> process-wide shared ngspice instance (PySpice 1.5 crashes on a second
> instance per process), so concurrent threads would interleave circuits on
> one instance. Each candidate pipeline gets its own process, library,
> ngspice instance and run dir (isolated artifacts); worker failures are
> captured per-candidate and a pool-startup failure degrades to serial.
> The NSGA-II reduced-order inner loop stays serial by design — it costs
> microseconds per candidate, less than any scheduling overhead.

------------------------------------------------------------------------

# Phase 16 --- Engineering Feedback & Failure Diagnosis

## Goal

Turn simulation failures into engineering decisions.

Example:

``` text
Tj = 125 °C
Limit = 100 °C

Loss breakdown:
Switching = 11 W
Conduction = 4 W
Inductor = 2 W
```

The agent should reason:

> Switching loss dominates, so simply choosing a lower-Rds_on MOSFET may
> provide limited benefit. Evaluate lower-Qg devices or lower switching
> frequency before increasing cooling.

## Important rule

The agent should not automatically change a design variable when the
change represents a meaningful engineering preference.

Instead:

1.  Make the change automatically if it is covered by an explicit
    optimization policy.
2.  Otherwise explain the proposed change and ask the user.

------------------------------------------------------------------------

# Phase 17 --- Interactive Schematic & Simulation UI

## Goal

Make the system interactive rather than purely batch-based.

The user should be able to:

-   view the schematic
-   edit components
-   connect/disconnect nodes
-   change values
-   run simulations
-   inspect waveforms
-   probe voltages/currents
-   compare simulations
-   inspect losses
-   inspect thermal results
-   request AI modifications

The UI and simulation backend must share the same unified Design
representation.

This phase is what transforms the system from an automation pipeline
into an actual **electronic design environment**.

------------------------------------------------------------------------

# Phase 18 --- AI Engineering Orchestrator

## Goal

Use an LLM as the engineering decision/orchestration layer.

The AI should:

``` text
Understand requirements
        ↓
Create design hypothesis
        ↓
Call engineering tools
        ↓
Inspect structured results
        ↓
Diagnose problems
        ↓
Propose changes
        ↓
Simulate again
        ↓
Validate
        ↓
Explain final design
```

The LLM should not replace deterministic engineering calculations.

Use deterministic tools for:

-   equations
-   component filtering
-   simulation
-   thermal solving
-   validation
-   constraint checking

Use the LLM for:

-   interpreting requirements
-   selecting among valid strategies
-   diagnosing failures
-   explaining trade-offs
-   coordinating tools
-   interacting with the user

## Reproducibility and provenance

Full LLM determinism cannot be guaranteed even at temperature=0. The
goal is an auditable trail, not bit-for-bit reproducibility:

-   Pin the orchestrator LLM model version (Phase 0) and log temperature
    and sampling settings for every design-decision call.
-   Every LLM decision that affects the design object must log its exact
    prompt, model version, and response as part of the design's
    provenance record (feeds Phase 19 memory).

## Hallucination guardrail

The LLM may only select components that exist in the Phase 1 database.
It must never synthesize or hallucinate a part number, datasheet value,
or rating. Component selection is performed by deterministic queries
against the database; the LLM chooses among database results, it does
not invent them.

------------------------------------------------------------------------

# Phase 19 --- Memory & Design Knowledge Base

## Goal

Store previous design/outcome pairs.

Example:

``` text
Specification
      +
Design parameters
      ↓
Simulation results
      ↓
Thermal results
      ↓
Final outcome
```

The system should remember:

-   successful designs
-   failed designs
-   component behavior
-   simulation conditions
-   optimization outcomes
-   user preferences

Memory should accelerate future design rather than override hard
constraints.

------------------------------------------------------------------------

# Phase 20 --- Human-in-the-Loop Approval

## Goal

Add approval gates only where they provide meaningful engineering
safety/value.

Recommended gates:

1.  First high-fidelity thermal simulation of a new design.
2.  Major specification relaxation/change.
3.  Significant topology change.
4.  Final component/BOM approval.
5.  Experimental hardware release.
6.  Analytical, SPICE, and CHT results disagree beyond a stated
    threshold (e.g. Tj estimates differing by more than a set margin) —
    the discrepancy investigation required by Phases 9 and 13 becomes
    an approval gate rather than a silent pass.

Routine low-risk optimization steps should remain autonomous.

## User-question policy

Approval requests must be concise:

``` text
Proposed change:
Reduce f_sw from 300 kHz → 200 kHz

Reason:
Switching loss is 68% of total loss.

Expected:
Tj: 117°C → ~96°C
Efficiency: 94.8% → ~96.1%

Trade-off:
L and C must be resized.
```

The user can approve, reject, or modify the proposal.

> **IMPLEMENTATION STATUS (2026-09-19, project decision):** Interactive
> approval gates are **descoped for this codebase**. The empty stub packages
> were removed (commit `55eafcc`). The engineering intent of the gates is
> covered by the system-wide *best-effort contract*: no design change with a
> material trade-off is ever applied silently — every mitigation lever the
> agent pulls (airflow, reselection, frequency), every measured-vs-spec miss,
> and every tool failure is recorded in `final.caveats` and rendered as red
> banners in the UI, and every run leaves an inspectable `manifest.json` +
> `design.json` + `llm_provenance.jsonl`. A future interactive gate would
> build on `pending_approval.json` in the run dir + a UI banner; revisit only
> if the platform gains concurrent human operators.

------------------------------------------------------------------------

# Phase 21 --- Experimental / Hardware Validation

## Goal

Eventually compare simulations against physical measurements.

Measure:

-   efficiency
-   voltage/current
-   ripple
-   component temperatures
-   transient response

Compare:

``` text
Simulation ↔ Hardware
```

Track error and use measured results to improve models.

This phase provides the strongest transition from a software simulation
project to a serious engineering research platform.

> **IMPLEMENTATION STATUS (2026-09-20):** Deferred until physical hardware
> exists — deliberately nothing built in software yet (no speculative
> schema). The import point is ready: every run's `manifest.json` carries
> the electrical/thermal numbers a bench comparison needs; when hardware
> arrives, add a `measured_results.json` sidecar per run + a deterministic
> sim-vs-measured comparison module feeding the Phase 19 memory.

------------------------------------------------------------------------

# Phase 22 --- Unified Engineering Output

## Goal

Produce one complete design package.

The output should contain:

``` text
design/
├── manifest.json
├── requirements.json
├── schematic/
├── netlist/
├── bom.csv
├── simulation/
├── losses/
├── thermal/
├── optimization/
├── reports/
└── validation/
```

The final report should explain:

-   what was designed
-   why it was designed that way
-   selected components
-   operating conditions
-   electrical performance
-   losses
-   thermal performance
-   optimization history
-   assumptions
-   unresolved uncertainties
-   validation status

------------------------------------------------------------------------

# Phase 23 --- Cross-Phase Validation & Regression

## Goal

Ensure that improvements to one phase do not silently break another.

Test:

``` text
Requirements
 → topology
 → design
 → netlist
 → SPICE
 → losses
 → geometry
 → OpenFOAM
 → optimization
 → output
```

Include regression cases covering:

-   buck
-   boost
-   buck-boost
-   light load
-   heavy load
-   thermal failure
-   infeasible specifications
-   component substitution
-   transient conditions

The entire suite should run automatically.

------------------------------------------------------------------------

# 4. Final Target Architecture

The completed platform should look conceptually like this:

``` text
                         USER
                           │
                           ▼
                ┌────────────────────┐
                │   AI ENGINEER       │
                │                    │
                │ reasoning          │
                │ planning            │
                │ diagnosis           │
                │ explanation         │
                └─────────┬──────────┘
                          │
                          ▼
                ┌────────────────────┐
                │ DESIGN OBJECT      │
                │                    │
                │ Requirements       │
                │ Topology            │
                │ Schematic           │
                │ Components          │
                │ Parameters          │
                │ Constraints         │
                │ Objectives          │
                └─────────┬──────────┘
                          │
           ┌──────────────┼──────────────┐
           ▼              ▼              ▼
      ANALYTICAL       ELECTRICAL      PHYSICAL
        ENGINE          ENGINE          ENGINE
           │              │              │
           │            SPICE         OpenFOAM
           │              │              │
           └──────────────┼──────────────┘
                          ▼
                 ┌─────────────────┐
                 │ VALIDATION      │
                 └────────┬────────┘
                          ▼
                 ┌─────────────────┐
                 │ OPTIMIZER       │
                 └────────┬────────┘
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
              PASS             ITERATE
                 │                 │
                 │        ┌────────┘
                 │        ▼
                 │   Ask user when
                 │   decision is ambiguous
                 │
                 ▼
          FINAL ENGINEERING
              PACKAGE
```

------------------------------------------------------------------------

# 5. Definition of Success

The project is successful when a user can provide a specification such
as:

> "Design a 48 V to 12 V, 20 A converter with at least 95% efficiency
> and Tj below 100 °C."

and the system can:

1.  Parse the specification.
2.  Identify missing requirements.
3.  Ask only the questions that materially affect engineering decisions.
4.  Select or propose an appropriate topology.
5.  Generate an initial design.
6.  Select real components with appropriate margins.
7.  Produce an editable schematic.
8.  Generate a valid SPICE model.
9.  Simulate electrical behavior.
10. Extract component-level losses.
11. Generate a physical board/component representation.
12. Run a thermal simulation.
13. Validate convergence and model consistency.
14. Optimize across relevant design variables.
15. Compare multiple feasible designs.
16. Explain why the final design was selected.
17. Ask for approval when an important engineering choice is ambiguous
    or consequential.
18. Produce a reproducible engineering package.
19. Eventually compare the simulation with hardware measurements.

The final system should therefore be understood not as:

> **"An AI that runs SPICE and OpenFOAM."**

but as:

> **"An AI-assisted electronic engineering environment that synthesizes,
> simulates, physically evaluates, validates, and optimizes
> power-electronics designs while keeping engineering decisions
> explicit, explainable, reproducible, and user-controlled."**
