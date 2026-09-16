# Project audit — 2026-09-16

Full audit of `pyspice_openfoam_agent`: every finding is listed with its root
cause, the fix applied, and how it is verified. Scope covered the electrical
path (sizing → selection → netlist → SPICE → losses → control loop), the
thermal path (electro-thermal + OpenFOAM CHT), the orchestrator (tools,
graph, design memory), tests, and project hygiene. All fixes are covered by
the unit suite (`tests/unit/test_audit_fixes.py` pins each one).

**Verification at time of writing:**
- `pytest tests/`: **151 passed / 7 env-gated skips** (was 125 passed / 7 failed /
  4 errors + a whole-suite collection crash).
- `scripts/topology_smoke.py`: **ALL PASSED** for buck, boost, buck_boost,
  every stage (spec → memory → sizing → screening → selection → netlist+lint+
  connectivity → SPICE steady-state+health → losses → control loop →
  electro-thermal).
- In the canonical container (`docker-agent:latest`): same smoke **with the
  full CHT solve** (`--thermal`) — ALL PASSED, per-device Tj extracted.

---

## 1. Test-environment infrastructure

### 1.1 PySpice crashes on the second ngspice instance in one process (critical)
`spice/runner.py` created a fresh `NgSpiceShared.new_instance()` per transient.
PySpice 1.5 re-executes its whole `ffi.cdef` block per instance, so the SECOND
simulation in any process raised `cffi.CDefError: duplicate declaration of
struct ngcomplex` — the smoke (3 topologies, one process) and the UI could
never run more than one design per session. **Fix:** one lazily-created,
process-wide instance with `remcirc` before each circuit load
(`spice/runner.py::_get_ngspice`).

### 1.2 Windows: ngspice DLL dependencies unresolvable (environment)
The shared `ngspice.dll` needs `sndfile.dll`/`samplerate.dll`/
`libomp140.x86_64.dll`; the Windows loader does not search the DLL's own
folder. **Fix:** runner bootstraps the process-local `PATH` +
`os.add_dll_directory` from PySpice's `LIBRARY_PATH` before the first dlopen;
the venv is provisioned with the official ngspice-47 DLL + share trees
(installation steps in `README.md`), and `ngspice_con.exe`/`ngspice.exe` are
added to the user PATH for the `ngspice -b` lint.

### 1.3 ngspice-47 benign stderr notes abort completed simulations (critical)
ngspice ≥ 44 prints `Note: <source>: dc value used for op instead of transient
time=0 value.` on stderr for PULSE sources with a DC prefix; PySpice flags any
non-`Warning:` stderr line as a command failure, so **completed** transients
raised `NgSpiceCommandError` (`Command 'run' failed`). **Fix:** the runner
accepts the run when a real transient plot with data was produced
(`_has_transient_plot`); a real failure still raises `SimulationError`.

### 1.4 Test-suite collection crashed on ngspice-less hosts
`test_spice.py::_ngspice_works` caught only `SimulationError`; the CDefError
from 1.1 escaped and aborted **collection of the entire suite**.
**Fix:** catch-all guard. Also `test_solver.py` passed a removed
`v_in_m_s` kwarg to `build_case` (the 4 real-CHT tests ERRORED in-container
instead of running — the documented "125 passed" baseline was stale), and
`test_reference` hard-failed instead of skipping without the `ngspice` binary.

### 1.5 Wrong JEDEC constant in a test
`test_board.py` expected 114.0×76.0 mm; JESD51-3 is **114.3×76.2 mm**. The
code was right; the test was fixed.

## 2. Orchestrator wiring

### 2.1 `run_thermal` loss reuse was dead code (duplicate simulation)
`tool_run_thermal` read `ctx.artifacts["_losses"]` — a key **nothing ever
wrote** — so the "reuse run_spice losses" guard never fired and every CHT run
re-ran the full transient with a different cycle cap. **Fix:** read
`ctx.losses` (cached by `tool_run_spice`).

### 2.2 `mitigate_thermal` was declared but unreachable
Schema present, advertised in the agent system prompt, but **no dispatch
branch** → every call returned `unknown tool`. The whole Phase 10 mitigation
decision tree was dead. **Fix:** `tool_mitigate_thermal` implemented (each
candidate re-enters the REAL pipeline — sizing → selection → SPICE → CHT — on
a child context via `thermal/mitigation.run_mitigation`) + dispatch wiring +
`run_thermal` now records `tj_max_k` as the mitigation baseline.

### 2.3 Explicit 0 m/s airflow silently replaced by the default
`tool_electro_thermal_converge` used `if v_in_m_s` — the same falsiness bug
previously fixed in `tool_run_thermal`, missed at this call site.
**Fix:** `is None` check (0 m/s = still air is a legitimate request).

### 2.4 Non-convergent electro-thermal results looked like success
`converged: False` payloads carried no `error` key → `dispatch` marked them
`ok=True` and the ReAct loop accepted the last, unconverged iterate (the
module's own docstring forbids this). **Fix:** non-convergence is error-tagged.

### 2.5 "Validated" CHT result with no temperatures
If temperature extraction found no matches, validation skipped the energy and
bounds checks and returned `valid=True`. **Fix:** `tool_run_thermal` rejects
empty `tj_per_device`; `validate_cht_result` treats a missing `tj_max` as
disqualifying.

### 2.6 Design memory polluted with empty outcomes
`graph.finalize` recorded every run unconditionally — the store held 12/24
`"outcome": {}` entries that `read_design_memory` fed to the LLM as "hits".
**Fix:** only runs with a thermal outcome are recorded.

### 2.7 Design memory store fragility
Non-atomic `write_text` (crash → truncated JSON), silent wipe of a corrupted
store on the next read, `OSError` unhandled, a 3-level parent search that
**split the store** for run dirs nested deeper (e.g.
`runs/topology_smoke/<stamp>/<topology>/`), and a docstring that misdescribed
the record schema and ordering. **Fix:** atomic temp+rename writes, corrupted
stores quarantined (`.corrupt-<ts>.json`) instead of erased, parent walk with
a sane bound, docstring corrected.

## 3. SPICE rig + control loop

### 3.1 Dead time inflated 5× and inconsistent between netlist and loss model
The uncommitted "hardening" change set `td = max(30 ns, 4×Miller-crossover)`
= **152 ns for the default MOSFET** — 7.6% of a 500 kHz period, three dead
windows per period — while `losses.py` still assumed a stale 30 ns constant.
Net consequence: the open-loop rig sat ~22% below the target Vout and every
downstream number (offset health gate, losses, efficiency) was taken at the
wrong operating point. **Fix:**
- `builder.dead_time_for()`: `max(30 ns, 2×crossover)` (turn-off + margin, a
  value a real gate driver would enforce), single source of truth;
- `losses.py` imports the same function and the shared `GATE_DRIVE_V/R`
  constants, and models the **3 dead windows/period** the gate phasing
  actually opens;
- switching-loss event currents are the ripple endpoints (valley at turn-on,
  peak at turn-off), not the window mean.

### 3.2 Open-loop rig now servo-tracked to the design operating point
An open-loop rig at the nominal duty always sits below the design Vout by the
static drops (dead-time diode clamp, DCR, Ron) — exactly what a closed-loop
controller compensates in hardware. **Fix:** `tool_run_spice` runs a bounded
deterministic **duty servo**: probes the rig, brackets the target Vout, and
converges with damped secant/bisection steps within the *renderable* trim
range (respecting the builders' minimum-pulse-width guard); the analytic
`suggested_charge_trim` (volt-second balance per topology) seeds the search;
provenance is recorded in `ctx.artifacts["duty_servo"]` and the netlist
header. Results: buck 4.97 V @ trim 1.11, boost/buck_boost within 0.5% of
target. The netlist on disk is the exact one simulated.

### 3.3 Capacitor ESL poisoned the rig with ±40 V non-physical spikes
With ideal zero-transition switches and zero-junction-cap diodes, the 0.5 nH
output-cap ESL sees unbounded `L·di/dt` at every commutation (worst on the
boost, where the switch ties directly to the output): ±40 V output spikes, a
false "shoot-through" health failure, and garbage ripple. ESL is milliohms at
500 kHz — pure toxin. **Fix:** `_cap_branch` no longer emits ESL (documented);
the legacy "robust impulse rejection" workarounds in the measurement code are
now belt-and-braces rather than load-bearing.

### 3.4 ngspice shared-library output-memory cap + fixed cycle budgets
Long transients crashed with `more than the memory available (25.52 MB)`
(~400k reported samples). Separately, fixed 460-cycle probes and an 800-cycle
steady-state cap could not contain soft-start + settling for slow output
poles (boost/buck_boost R·C τ ≈ 140 cycles) — the servo chased a still-rising
reading. **Fix:** adaptive `points_per_cycle` keeps every run ≤ 200k reported
samples; probe and final-run budgets scale with the output tank's settling
time (`_settling_cycles`).

### 3.5 Control-loop margins: unmodeled PWM delay overstated phase margin
Margins were s-domain only; any real implementation adds a modulator delay
(~0.5·Tsw average, up to ~1.5·Tsw digital) ≈ −18° of phase at buck's
0.1·fsw crossover. **Fix:** exact-magnitude all-pass delay term (first-order
Padé, `delay_periods=0.5` default) in the loop gain, plus:
- **phase-targeted Type III design**: the double zero is solved by bisection
  so the delay-inclusive phase at the target crossover delivers the 45° gate
  +2° headroom (the old fixed zeros-at-LC placement could not reach the gate
  at any crossover for several real plants);
- pole 1 cancels the ESR zero exactly (the old `max(f_esr, 4fc)` broke the
  cancellation for ceramic caps, taxing PM ~11° for nothing);
- the 1 kHz crossover floor can no longer lift the target above the RHP-zero
  guard (boost/buck_boost);
- the tank Q clamp is applied AFTER the D' scaling (it previously let small-D'
  plants fall below the intended floor);
- margin extraction reports the WORST crossing (conditionally stable loops),
  handles exact-zero samples, and sweeps a wider band.

### 3.6 Netlist validation and lint
`validate.py` parsed the TITLE line as an element (safe only while titles
began with a non-element letter), ignored `+` continuation lines and `;`
comments, and carried a no-op `gate_` branch — rewritten. `lint_netlist`
judged success by the substring "error" alone — now exit code + explicit
ngspice failure markers (fails closed on known failure phrasings).

## 4. Thermal / reduced-order models

### 4.1 Pareto optimizer ranked every topology with buck physics
`reduced_order_losses` pinned switching Vds at **12 V** (a 48 V design
under-predicted switching loss ~4×) and used a blanket 1.5× conduction factor;
the inner loop sized ALL candidates with a buck-only `_quick_size` (returning
garbage for boost/buck_boost), built then ignored a `spec_kwargs` dict, and
never checked the L decision variable against L_min. Ambient was 27 °C while
every other thermal module used 25 °C. **Fix:** topology-aware `v_block` +
`conduction_factor()`, engine-based topology-constrained sizing in the loop,
L_min screening, ambient unified at 25 °C, negative-loss clamp in
`reduced_order_tj`.

### 4.2 Electro-thermal loop
The blanket 1.5× conduction factor was wrong for every topology (buck is
exactly 1.0 = D+(1−D); boost/buck_boost carry Iout/(1−D) through the
switches → 1/(1−D)², ×2 for four switches) — replaced by
`electro_thermal.conduction_factor(topology, D)`. The fixed point gained
**adaptive damping** (relaxation halves when the residual grows; MAX_ITER
5→8). Note: for linearized loop gain ≥ 1 no positive fixed point exists
(thermal runaway) — the loop now honestly reports that instead of masking it.

### 4.3 CHT solver
Each relaxation attempt **overwrote** `solve.log` (divergence evidence
destroyed); success required only `returncode==0` + ≤3 NaN lines — a log the
validator (any NaN = invalid) was guaranteed to reject; `endTime` reaching was
never checked; solids were never relaxed while real logs showed solid
temperatures blowing up. **Fix:** per-attempt `solve_level<N>.log` preserved,
any-NaN fails (ladder still retries), endTime-reached check, and the
relaxation ladder now also under-relaxes every solid region's energy
equation.

### 4.4 Validation docstring vs behavior
The residual-trend check claimed to use p_rgh but in practice runs on `h`
(the energy equation is solved in every solid per outer iteration) — comment
corrected; per-equation comparison retained (comparing residuals of different
equations is meaningless).

## 5. Structure / hygiene

- **Dead code:** `sizing/topology_select.py` was production-dead — its
  multi-criteria scorer now generates an advisory sizing note when the
  voltage ratio falls in the ambiguous 0.95–1.05 band (the ratio rule
  defaults to the most expensive option there); `thermal/extraction.py` lost
  a duplicated line and an unused `_latest_time` helper; `np.trapz` shim made
  consistent with `losses.py`; empty placeholder packages removed
  (`orchestrator/hitl/`, `orchestrator/parallel/`, `mitigation/`).
- **Ghost dependencies:** `jinja2` and `foamlib` pinned in `pyproject.toml`
  and asserted by the Dockerfile import check, imported nowhere — removed
  (Dockerfile check updated accordingly).
- **config drift:** `SOLVER_BIN` named `chtMultiRegionFoam` while the solver
  invokes `chtMultiRegionSimpleFoam` — fixed.
- **Model slug drift:** `.env.example` vs `graph.py`/`app.py` defaults —
  aligned on `deepseek/deepseek-v4-flash-0731`.
- **Stale docs:** `builder.py` module header still described the buck-boost
  as a 2-switch magnitude-mode circuit; `control_loop/design.py` claimed a
  Type II option and a scipy backend; `topology_smoke.py` bypassed the sizing
  tool over a comment claiming the tool had no `topology` parameter (it does)
  — smoke now drives the real tool path for all topologies, and its
  buck_boost case was retuned 300→500 kHz (at 300 kHz L_min ≈ 48 µH and NO
  library inductor satisfied the L/Isat/Irms margins simultaneously — the
  case was unsatisfiable by construction). Missing `README.md` written;
  `PROJECT_STRUCTURE.md` (referenced files that never existed) rewritten;
  `CLAUDE.md` baseline updated.

## 6. Known limitations (documented, not changed)

- buck_boost switching loss uses `max(Vin, Vout)` as blocking voltage for all
  four dies (the output pair blocks only Vout) — conservative.
- Crossover loss is the first-order TI-Lakkas model with 1–200 ns clamps;
  `I_rms_sw` screening ignores ripple; the reduced-order airflow scaling
  applies to the whole junction-to-ambient path — all documented heuristics.
- Digital implementations with a full-sample compute delay should call the
  control analysis with `delay_periods≈1.5` (default models the unavoidable
  0.5·Tsw modulator delay only).
- `memory_store/design_memory.json` retains its historical noise entries
  (pre-policy); future runs append only real outcomes.
- The CHT tier remains container-only by design (Linux OpenFOAM v2406).
