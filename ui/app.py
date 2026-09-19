"""Phase 15: Streamlit UI — one app, four tabs (Pipeline / Circuit / Thermal /
Results), sidebar spec input + run control.

Run:  streamlit run ui/app.py   (inside the docker-agent container)
The UI drives the orchestrator (scripted or live OpenRouter per env) and
polls the RunState JSON for live progress. Views read from ToolContext
artifacts.

Audit fixes applied:
- APP-1: runs are launched in a background thread so the UI stays responsive
  and auto-refreshes to show live pipeline progress.
- APP-2: existing-run dropdown filters to dirs containing state.json.
- APP-3: missing metrics show "N/A", not 0.0%.
- APP-5: removed dead _run_dir injection.
- APP-6: renamed mock to "scripted (no LLM)" to set expectations.
- APP-7: ripple_ratio exposed in the sidebar.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

# make src importable when running from the repo root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import streamlit as st

from pyspice_openfoam_agent.ui.run_state import RunState

st.set_page_config(page_title="DC-DC Synthesizer", page_icon="⚡", layout="wide")

RUNS_DIR = REPO / "runs"


# ---------------- helpers ----------------


def _valid_runs() -> list[str]:
    """Dirs in runs/ that contain a state.json (actual orchestrated runs)."""
    if not RUNS_DIR.exists():
        return []
    return sorted(
        [d.name for d in RUNS_DIR.iterdir()
         if d.is_dir() and (d / "state.json").exists()],
        reverse=True,
    )


def _fmt_metric(value, suffix: str = "", multiplier: float = 1.0) -> str:
    """Format a metric for display; N/A if missing/None."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * multiplier:.1f}{suffix}"
    except (TypeError, ValueError):
        return "N/A"


# ---------------- sidebar ----------------


@st.cache_resource(show_spinner=False)
def _library():
    from pyspice_openfoam_agent.library.loader import load_library

    return load_library()


def sidebar() -> dict:
    st.sidebar.title("⚡ DC-DC Synthesizer")
    lib = _library()
    with st.sidebar.form("spec_form"):
        st.subheader("Converter spec")
        vin = st.number_input("Vin (V)", 1.0, 400.0, 12.0, 0.5)
        vout = st.number_input("Vout (V)", 0.5, 100.0, 5.0, 0.5)
        iout = st.number_input("Iout (A)", 0.1, 50.0, 5.0, 0.5)
        fsw = st.number_input("f_sw (kHz)", 50.0, 1000.0, 500.0, 50.0)
        vrip_m = st.number_input("Vripple (mV)", 1.0, 500.0, 50.0, 5.0)
        ripple_ratio = st.slider("Inductor ripple ratio", 0.10, 0.50, 0.30, 0.05,
                                  help="Typical 0.2-0.4. Higher = smaller L, more ripple.")
        st.subheader("Input range (optional)")
        c1, c2 = st.columns(2)
        vin_lo = c1.number_input("Vin min (V)", 0.0, 400.0, 0.0, 0.5,
                                 help="Set BOTH endpoints > 0 to size at both "
                                      "corners (worst of each); design pins at Vin max.")
        vin_hi = c2.number_input("Vin max (V)", 0.0, 400.0, 0.0, 0.5)
        st.subheader("Part overrides (optional)")
        mos_pick = st.selectbox("MOSFET", ["<automatic>"] + sorted(lib.mosfets))
        ind_pick = st.selectbox("Inductor", ["<automatic>"] + sorted(lib.inductors))
        cap_pick = st.selectbox("Capacitor", ["<automatic>"] + sorted(lib.capacitors))
        submitted = st.form_submit_button("Run design", use_container_width=True)
    mode = st.sidebar.radio(
        "LLM mode",
        ("scripted (no LLM)", "OpenRouter (DeepSeek Flash)"),
        index=0,
        help="scripted: deterministic flow (no API); OpenRouter: live agent "
             "(needs OPENROUTER_API_KEY)",
    )
    existing = st.sidebar.selectbox(
        "Or view an existing run",
        ["<new run>"] + _valid_runs(),
    )
    task = {"Vin": vin, "Vout": vout, "Iout": iout, "fsw_khz": fsw,
            "Vripple": vrip_m / 1000.0, "ripple_ratio": ripple_ratio}
    if 0.0 < vin_lo <= vin_hi and vin_lo < vin:
        task["vin_min"] = vin_lo
        task["vin_max"] = vin_hi
    for key, pick in (("mosfet_override", mos_pick), ("inductor_override", ind_pick),
                      ("capacitor_override", cap_pick)):
        if pick != "<automatic>":
            task[key] = pick  # library-validated by the tool; free text impossible
    if submitted:
        return {"action": "run", "task": task, "mode": mode}
    if existing != "<new run>":
        return {"action": "view", "run_id": existing}
    return {"action": "idle"}


# ---------------- tabs ----------------


def tab_pipeline(state: dict | None) -> None:
    st.header("Pipeline")
    if not state:
        st.info("No run selected — start one from the sidebar.")
        return
    status = state.get("status", "?")
    color = {"done": "🟢", "running": "🟡", "error": "🔴"}.get(status, "⚪")
    st.markdown(
        f"**Status:** {color} `{status}`  ·  "
        f"step {state.get('step', 0)}/{state.get('max_steps', '?')}"
    )
    if state.get("error"):
        st.error(state["error"])
    history = state.get("history", [])
    if not history:
        st.info("No tool calls yet.")
        return
    for h in history:
        icon = "✅" if h.get("ok") else "❌"
        with st.expander(f"{icon} {h['tool']}", expanded=False):
            st.json(h.get("summary", {}))


def tab_circuit(state: dict | None, artifacts: dict) -> None:
    st.header("Circuit")
    if not artifacts.get("components"):
        st.info("No design yet — run the pipeline first.")
        return
    comps = artifacts["components"]
    sizing = artifacts.get("sizing", {})

    sch = artifacts.get("schematic_png")
    if sch and Path(sch).exists():
        st.image(str(sch),
                 caption=f"{sizing.get('topology', '?')} — {comps['mosfet']} / {comps['inductor']}")
    else:
        st.caption("schematic appears after run_spice completes")

    netlist = artifacts.get("netlist")
    if netlist and Path(netlist).exists():
        with st.expander("SPICE netlist", expanded=False):
            st.code(Path(netlist).read_text(), language="text")

    wf = artifacts.get("waveforms_png")
    if wf and Path(wf).exists():
        st.image(str(wf), caption="Vout waveform (startup + steady-state zoom)")


def tab_thermal(state: dict | None, artifacts: dict) -> None:
    st.header("Thermal (OpenFOAM CHT)")
    thermal = artifacts.get("thermal")
    if not thermal:
        st.info("No thermal solve yet.")
        return
    tj = thermal.get("tj_per_device_C", {})
    if tj:
        cols = st.columns(len(tj))
        for col, (dev, t) in zip(cols, sorted(tj.items())):
            col.metric(dev, f"{t:.1f} °C")
        limit = 150.0
        worst = max(tj.values())
        st.progress(min(worst / limit, 1.0),
                     text=f"Tj_max {worst:.1f} / {limit:.0f} °C limit")

    mv = artifacts.get("tj_multiview_png")
    if mv and Path(mv).exists():
        st.subheader("Annotated multi-view")
        st.image(str(mv), caption="iso / top / front / side temperature field (K)")
    else:
        png = artifacts.get("tj_png")
        if png and Path(png).exists():
            st.image(str(png), caption="Temperature field (solid regions)")
        else:
            st.caption("temperature render appears after the CHT solve completes")

    html = artifacts.get("tj_3d_html")
    if html and Path(html).exists():
        st.subheader("Interactive 3D")
        st.info("Self-contained three.js viewer (no server/Python needed). "
                "Orbit: left-drag rotate · scroll zoom · right-drag pan.")
        # 1) live embed straight into the page (works over HTTP)
        with st.expander("▶  Interactive 3D (embed)", expanded=True):
            st.components.v1.html(Path(html).read_text(encoding="utf-8"), height=600, scrolling=False)
        # 2) self-contained file you can save and open in a new tab, offline
        st.caption(f"Run directory: `{Path(html).parent}`")
        st.download_button(
            "Download thermal_3d.html (open in a new tab)",
            data=Path(html).read_bytes(),
            file_name="thermal_3d.html",
            mime="text/html",
            use_container_width=True,
        )


def tab_results(state: dict | None, artifacts: dict) -> None:
    st.header("Results")
    final = (state or {}).get("final")
    if final:
        caveats = final.get("caveats") or []
        for c in caveats:
            st.error(f"Tool failure during this run — the summary below may "
                     f"overstate the design: {c}")
        st.success(final.get("summary", "run complete"))
    spice = artifacts.get("spice")
    if spice:
        st.subheader("Electrical")
        c1, c2, c3 = st.columns(3)
        eff = spice.get("efficiency")
        c1.metric("efficiency", _fmt_metric(eff, "%", 100) if eff is not None else "N/A")
        rip = spice.get("ripple_V")
        c2.metric("ripple", _fmt_metric(rip, " mV", 1000) if rip is not None else "N/A")
        loss = spice.get("losses_W")
        c3.metric("total loss", _fmt_metric(loss, " W") if loss is not None else "N/A")
    # Phase 6 control-loop verdict (goals: margins computed, not LLM-judged)
    cl = artifacts.get("control_loop")
    if cl:
        st.subheader("Control loop (Phase 6)")
        c1, c2, c3 = st.columns(3)
        c1.metric("phase margin",
                  _fmt_metric(cl.get("phase_margin_deg"), "°") if cl.get("phase_margin_deg") is not None else "N/A")
        gm = cl.get("gain_margin_db")
        c2.metric("gain margin", "∞" if gm and gm >= 40 else _fmt_metric(gm, " dB"))
        # artifact key is crossover_hz (Group C fix: the old crossover_kHz
        # read never matched, so this metric always rendered N/A)
        fc_hz = cl.get("crossover_hz")
        c3.metric("crossover",
                  _fmt_metric(fc_hz, " kHz", 1e-3) if fc_hz is not None else "N/A")
        if cl.get("passed"):
            st.success(f"PASS — {cl.get('compensator', '?')} compensator")
        else:
            st.error(f"FAIL — {cl.get('compensator', '?')} compensator")
        if cl.get("reasons"):
            st.caption(" · ".join(cl["reasons"]))
    # electro-thermal convergence (the AUTHORITATIVE self-consistent Tj).
    # Task: show the final converged value, not the seed iteration.
    et = artifacts.get("electro_thermal")
    if et and et.get("final_tj_C") is not None:
        st.subheader("Junction temperature (electro-thermal, converged)")
        c1, c2 = st.columns(2)
        c1.metric("Tj (final, converged)", f"{et['final_tj_C']:.1f} °C")
        c2.metric("iterations", et.get("iterations", "N/A"))
        if et.get("converged"):
            st.success("converged — is the self-consistent operating Tj "
                       "(weakly-coupled reduced-order model)")
        else:
            st.error("convergence FAILED — validation failure, investigate")
        if et.get("trace"):
            with st.expander("Fixed-point trace", expanded=False):
                st.code("\n".join(et["trace"]), language="text")

    thermal = artifacts.get("thermal")
    if thermal:
        st.subheader("Full CHT solve (OpenFOAM, reference)")
        st.markdown(
            "The table below is the full 3D conjugate-heat-transfer solve at the "
            "same operating point — a cross-check on the electro-thermal Tj above."
        )
        st.json(thermal.get("tj_per_device_C", {}))

    # Phase 6/8 transient step tests (open-loop plant response)
    stp = artifacts.get("step_tests")
    if stp:
        st.subheader("Transient step tests (open-loop plant)")
        rows = []
        for kind in ("load_step", "input_step"):
            d = stp.get(kind)
            if not d or "error" in d:
                continue
            rows.append({
                "test": kind,
                "under/overshoot V": f"{d.get('undershoot_V', 0):.3f} / {d.get('overshoot_V', 0):.3f}",
                "settling": (f"{d.get('settling_time_us')} µs" if d.get("settled") else "not settled"),
                "V before → after": f"{d.get('v_before')} → {d.get('v_final')} V",
            })
        if rows:
            st.dataframe(rows, use_container_width=True)
            st.caption(stp.get("note", ""))

    # Phase 8 operating-condition sweep
    sw = artifacts.get("sweep")
    if sw and sw.get("points"):
        st.subheader(f"Operating-point sweep ({sw.get('mode', '?')})")
        st.dataframe(sw["points"], use_container_width=True)
        st.caption(sw.get("note", ""))

    # Phase 14 Pareto finalists (CHT-verified)
    po = artifacts.get("pareto")
    if po and po.get("finalists"):
        st.subheader("Pareto finalists (NSGA-II + CHT verification)")
        st.dataframe(po["finalists"], use_container_width=True)


# ---------------- compare tab (Phase 17: compare simulations) ----------------


def tab_compare() -> None:
    st.header("Compare runs")
    st.caption("Pick any runs (including the live one) to compare side by side. "
               "Missing stages show as blanks.")
    runs = _valid_runs()
    if not runs:
        st.info("No completed runs yet.")
        return
    chosen = st.multiselect("Runs to compare", runs,
                            default=runs[:2])
    rows = []
    for rid in chosen:
        state = RunState(RUNS_DIR / rid).read() or {}
        arts = state.get("artifacts", {}) or {}
        sp = arts.get("spice", {}) or {}
        cl = arts.get("control_loop", {}) or {}
        th = arts.get("thermal", {}) or {}
        comps = arts.get("components", {}) or {}
        tj_map = th.get("tj_per_device_C", {}) or {}
        rows.append({
            "run": rid,
            "status": state.get("status"),
            "topology": (arts.get("sizing", {}) or {}).get("topology"),
            "mosfet": comps.get("mosfet"),
            "inductor": comps.get("inductor"),
            "capacitor": comps.get("capacitor"),
            "eff": sp.get("efficiency"),
            "ripple mV": (round(sp["ripple_V"] * 1e3, 2)
                          if sp.get("ripple_V") is not None else None),
            "loss W": sp.get("losses_W"),
            "PM °": cl.get("phase_margin_deg"),
            "GM dB": cl.get("gain_margin_db"),
            "Tj max °C": (round(max(tj_map.values()), 1) if tj_map else None),
        })
    if rows:
        st.dataframe(rows, use_container_width=True, hide_index=True)


# ---------------- background run ----------------


def _run_in_background(task: dict, mode: str, run_id: str) -> None:
    """Execute run_agent in a daemon thread so the UI stays responsive."""
    from pyspice_openfoam_agent.orchestrator.graph import run_agent

    run_dir = RUNS_DIR / run_id
    try:
        provider = {"scripted (no LLM)": "mock",
                    "OpenRouter (DeepSeek Flash)": "openrouter"}[mode]
        model = "deepseek/deepseek-v4-flash-0731"
        if provider == "mock":
            size_args = {
                "Vin": task["Vin"], "Vout": task["Vout"],
                "Iout": task["Iout"], "fsw_khz": task["fsw_khz"],
                "Vripple": task["Vripple"],
            }
            if task.get("ripple_ratio"):
                size_args["ripple_ratio"] = task["ripple_ratio"]
            if task.get("vin_min") and task.get("vin_max"):
                size_args.update(vin_min=task["vin_min"], vin_max=task["vin_max"])
            select_args = {}
            for key, arg in (("mosfet_override", "mosfet"),
                             ("inductor_override", "inductor"),
                             ("capacitor_override", "capacitor")):
                if task.get(key):
                    select_args[arg] = task[key]
            mock = [
                {"tool_calls": [{"name": "size_converter", "args": size_args}]},
                {"tool_calls": [{"name": "select_components", "args": select_args}]},
                {"tool_calls": [{"name": "build_netlist", "args": {}}]},
                {"tool_calls": [{"name": "analyze_control_loop", "args": {}}]},
                {"tool_calls": [{"name": "run_spice", "args": {}}]},
                {"tool_calls": [{"name": "electro_thermal_converge", "args": {"v_in_m_s": 1.0}}]},
                {"tool_calls": [{"name": "run_thermal", "args": {"v_in_m_s": 1.0}}]},
                {"done": True, "final": {"summary":
                    f"{task.get('Vout')} V design pipeline complete — scripted (no LLM) mode. "
                    "Run the OpenRouter mode for a full LLM-written engineering summary."}},
            ]
            run_agent(task, run_dir, mock_responses=mock)
        else:
            run_agent(task, run_dir, model=model, provider=provider)
    except Exception as e:
        rs = RunState(run_dir)
        rs.finish(None, error=str(e))


# ---------------- main ----------------


def main() -> None:
    action = sidebar()
    state = None
    run_id = None

    if action["action"] == "run":
        import uuid

        run_id = f"run_{uuid.uuid4().hex[:8]}"
        st.session_state["active_run"] = run_id  # survive reruns (audit fix)
        run_dir = RUNS_DIR / run_id
        rs = RunState(run_dir)
        rs.init(action["task"], max_steps=12)
        st.toast(f"starting run {run_id} ({action['mode']} mode)")
        # APP-1 fix: launch in a background thread, poll state.json
        t = threading.Thread(
            target=_run_in_background,
            args=(action["task"], action["mode"], run_id),
            daemon=True,
        )
        t.start()
        # surface the fresh (seed) state immediately so the first paint is the
        # new run, not a leftover previous one
        state = rs.read()
    elif action["action"] == "view":
        # explicit view: always read that run's FINAL state.json
        run_id = action["run_id"]
        st.session_state["active_run"] = run_id
        state = RunState(RUNS_DIR / run_id).read()

    # on reruns (auto-refresh), recover the active run from session state
    if run_id is None and st.session_state.get("active_run"):
        run_id = st.session_state["active_run"]

    if run_id:
        sp = RUNS_DIR / run_id / "state.json"
        if sp.exists():
            state = RunState(RUNS_DIR / run_id).read()

    artifacts = (state or {}).get("artifacts", {})
    t1, t2, t3, t4, t5 = st.tabs(
        ["Pipeline", "Circuit", "Thermal", "Results", "Compare"])
    with t1:
        tab_pipeline(state)
    with t2:
        tab_circuit(state, artifacts)
    with t3:
        tab_thermal(state, artifacts)
    with t4:
        tab_results(state, artifacts)
    with t5:
        tab_compare()

    # auto-refresh while a run is live (APP-1: now works because the run
    # is in a background thread; the UI thread is free to rerun)
    if state and state.get("status") == "running":
        import time

        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()