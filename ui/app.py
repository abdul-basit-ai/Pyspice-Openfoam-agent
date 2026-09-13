"""Phase 15: Streamlit UI — one app, four tabs (Pipeline / Circuit / Thermal /
Results), sidebar spec input + run control.

Run:  streamlit run ui/app.py   (inside the docker-agent container)
The UI drives the orchestrator (mock or live Gemini per env) and polls the
RunState JSON for live progress. Views read from ToolContext artifacts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# make src importable when running from the repo root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import streamlit as st

from pyspice_openfoam_agent.ui.run_state import RunState

st.set_page_config(page_title="DC-DC Synthesizer", page_icon="⚡", layout="wide")

RUNS_DIR = REPO / "runs"


# ---------------- sidebar ----------------


def sidebar() -> dict:
    st.sidebar.title("⚡ DC-DC Synthesizer")
    with st.sidebar.form("spec_form"):
        st.subheader("Converter spec")
        vin = st.number_input("Vin (V)", 1.0, 400.0, 12.0, 0.5)
        vout = st.number_input("Vout (V)", 0.5, 100.0, 5.0, 0.5)
        iout = st.number_input("Iout (A)", 0.1, 50.0, 5.0, 0.5)
        fsw = st.number_input("f_sw (kHz)", 50.0, 1000.0, 500.0, 50.0)
        vrip_m = st.number_input("Vripple (mV)", 1.0, 500.0, 50.0, 5.0)
        submitted = st.form_submit_button("Run design", use_container_width=True)
    mode = st.sidebar.radio("LLM mode", ("mock", "live Gemini"), index=0,
                            help="mock: scripted flow; live: real Gemini API (needs GEMINI_API_KEY)")
    existing = st.sidebar.selectbox(
        "Or view an existing run",
        ["<new run>"] + sorted([d.name for d in RUNS_DIR.iterdir() if d.is_dir()],
                               reverse=True) if RUNS_DIR.exists() else ["<new run>"],
    )
    task = {"Vin": vin, "Vout": vout, "Iout": iout, "fsw_khz": fsw,
            "Vripple": vrip_m / 1000.0}
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
    st.markdown(f"**Status:** {color} `{status}`  ·  step {state.get('step', 0)}/{state.get('max_steps', '?')}")
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
    task = state.get("task", {}) if state else {}

    # schematic + waveforms are generated during run_spice (artifacts pointers)
    sch = artifacts.get("schematic_png")
    if sch and Path(sch).exists():
        st.image(str(sch), caption=f"{sizing.get('topology', '?')} — {comps['mosfet']} / {comps['inductor']}")
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
    if tj:
        worst = max(tj.values())
        st.progress(min(worst / limit, 1.0), text=f"Tj_max {worst:.1f} / {limit:.0f} °C limit")
    png = artifacts.get("tj_png")
    if png and Path(png).exists():
        st.image(str(png), caption="Temperature field (solid regions)")
    else:
        st.caption("3D render unavailable (needs OSMesa in the container image) — table above is the source of truth.")


def tab_results(state: dict | None, artifacts: dict) -> None:
    st.header("Results")
    final = (state or {}).get("final")
    if final:
        st.success(final.get("summary", "run complete"))
    spice = artifacts.get("spice")
    if spice:
        st.subheader("Electrical")
        c1, c2, c3 = st.columns(3)
        c1.metric("efficiency", f"{spice.get('efficiency', 0) * 100:.1f}%")
        c2.metric("ripple", f"{spice.get('ripple_V', 0) * 1000:.1f} mV")
        c3.metric("total loss", f"{spice.get('losses_W', 0):.2f} W")
    thermal = artifacts.get("thermal")
    if thermal:
        st.subheader("Thermal")
        st.json(thermal.get("tj_per_device_C", {}))
    # manifest download
    manifest = artifacts.get("manifest_path")
    if manifest and Path(manifest).exists():
        st.download_button("Download manifest.json", Path(manifest).read_text(),
                           file_name="manifest.json")


# ---------------- main ----------------


def main() -> None:
    action = sidebar()
    run_state: RunState | None = None
    state = None
    run_id = None

    if action["action"] == "view":
        run_id = action["run_id"]
        run_state = RunState(RUNS_DIR / run_id)
        state = run_state.read()
    elif action["action"] == "run":
        import uuid

        run_id = f"run_{uuid.uuid4().hex[:8]}"
        run_state = RunState(RUNS_DIR / run_id)
        run_state.init(action["task"], max_steps=12)
        # launch the orchestrator in mock or live mode
        from pyspice_openfoam_agent.orchestrator.graph import run_agent

        st.toast(f"starting run {run_id} ({action['mode']} mode)")
        try:
            if action["mode"] == "mock":
                mock = [
                    {"tool_calls": [{"name": "size_converter", "args": {
                        "Vin": action["task"]["Vin"], "Vout": action["task"]["Vout"],
                        "Iout": action["task"]["Iout"], "fsw_khz": action["task"]["fsw_khz"],
                        "Vripple": action["task"]["Vripple"]}}]},
                    {"tool_calls": [{"name": "select_components", "args": {}}]},
                    {"tool_calls": [{"name": "build_netlist", "args": {}}]},
                    {"tool_calls": [{"name": "run_spice", "args": {}}]},
                    {"tool_calls": [{"name": "run_thermal", "args": {}}]},
                    {"done": True, "final": {"summary": "design complete"}},
                ]
                run_agent(action["task"], RUNS_DIR / run_id, mock_responses=mock)
            else:
                run_agent(action["task"], RUNS_DIR / run_id)
            state = run_state.read()
        except Exception as e:
            run_state.finish(None, error=str(e))
            state = run_state.read()

    if run_id:
        state = state or (run_state.read() if run_state else None)
        if state:
            state["_run_dir"] = str(RUNS_DIR / run_id)

    artifacts = (state or {}).get("artifacts", {})
    t1, t2, t3, t4 = st.tabs(["Pipeline", "Circuit", "Thermal", "Results"])
    with t1:
        tab_pipeline(state)
    with t2:
        tab_circuit(state, artifacts)
    with t3:
        tab_thermal(state, artifacts)
    with t4:
        tab_results(state, artifacts)

    # auto-refresh while a run is live
    if state and state.get("status") == "running":
        import time

        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    main()