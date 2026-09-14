"""Phase 11: LangGraph ReAct orchestrator.

Reason -> act -> observe loop over the Phase 11 tool layer, driven by an LLM
function-calling via OpenRouter (user decision: OpenRouter API; recorded mocks in tests so the
64-test suite stays free/deterministic — user decision: always-LLM, mocked
in tests, no deterministic fallback).

State contract (compact per the plan's context rule): the graph state holds
only task framing, the tool-call transcript (compact payloads), and derived
artifacts pointers. Raw waveforms/logs never enter state.

Graph shape:
  START -> reason -> (route) -> act -> observe -> reason ... -> END
  reason: the LLM decides the next tool call from the transcript
  route:  tool_calls present? -> act : -> finalize
  act:    dispatch the requested tool(s)
  observe: append compact results to the transcript (merged into act node)
  finalize: assemble the Phase 12 bundle inputs + record design memory
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, TypedDict

from pyspice_openfoam_agent.orchestrator.tools import (
    TOOL_SCHEMAS,
    ToolContext,
    ToolResult,
    dispatch,
    spec_signature,
)


class OrchestratorError(RuntimeError):
    """Agent run failed structurally (not a tool failure)."""


@dataclass
class AgentConfig:
    run_dir: Path
    model: str = "deepseek/deepseek-v4-flash-0731"
    provider: str = "openrouter"  # OpenRouter is the sole LLM provider
    max_steps: int = 12


def _openai_tools() -> list[dict]:
    """Convert TOOL_SCHEMAS to OpenAI-style function declarations."""
    decls = []
    for t in TOOL_SCHEMAS:
        decls.append({"name": t["name"], "description": t["description"],
                      "parameters": t["parameters"]})
    return [{"function_declarations": decls}]


SYSTEM_PROMPT = """You are an autonomous DC-DC converter design agent.
Goal: given a converter spec (Vin, Vout, Iout, fsw, ripple), produce a
verified design: validated sizing -> real components -> SPICE-verified
steady state -> thermal solve with junction temperatures below 150 degC.

Standard flow: read_design_memory -> size_converter -> select_components ->
build_netlist -> run_spice -> run_thermal (mitigate_thermal if Tj too high).
Stop when the thermal result is in hand; report the key numbers.

Rules:
- One tool call per step; observe each result before deciding.
- If a tool returns an error, adapt or stop with a clear reason.
- Do not invent part numbers: only what the tools return.
"""


class AgentState(TypedDict):
    task: dict  # the spec from the user
    transcript: list[dict]  # [{role, content}] compact
    tool_calls: list[dict]  # pending calls for the act node
    step: int
    done: bool
    final: dict | None
    error: str | None


def make_graph(config: AgentConfig, ctx: ToolContext, client=None, mock_responses: list[dict] | None = None, run_state=None):
    """Build the agent graph. `client` and `mock_responses` are for tests:
    mock mode replays the recorded responses instead of calling the LLM."""
    from langgraph.graph import END, StateGraph

    def reason(state: AgentState) -> dict:
        step = state["step"]
        if step >= config.max_steps:
            return {"done": True, "error": "max steps reached"}
        if mock_responses is not None:
            if step >= len(mock_responses):
                return {"done": True}
            resp = mock_responses[step]
        else:
            resp = _call_openrouter(config.model, state)
        calls = resp.get("tool_calls", [])
        return {"tool_calls": calls, "done": resp.get("done", False),
                "final": resp.get("final")}

    def route(state: AgentState) -> str:
        if state["done"] or state.get("final"):
            return "finalize"
        if not state["tool_calls"]:
            return "finalize"
        return "act"

    def act(state: AgentState) -> dict:
        transcript = list(state["transcript"])
        for call in state["tool_calls"]:
            name = call["name"]
            args = call.get("args", {})
            result: ToolResult = dispatch(ctx, name, args)
            transcript.append({"role": "tool", "tool": name, "args": args,
                               "ok": result.ok, "result": result.payload})
            if run_state is not None:
                run_state.log_tool(name, result.ok, result.payload,
                                   ctx.artifacts, state["step"] + 1)
        return {"transcript": transcript, "tool_calls": [], "step": state["step"] + 1}

    def finalize(state: AgentState) -> dict:
        final = dict(state.get("final") or {})
        final.setdefault("artifacts", ctx.artifacts if ctx else {})
        # record design memory (Phase 11a)
        if ctx and ctx.spec:
            try:
                from pyspice_openfoam_agent.orchestrator.memory.design_memory import DesignMemory

                mem = DesignMemory(ctx.run_dir)
                mem.record_outcome(
                    ctx.spec.Vin, ctx.spec.Vout, ctx.spec.Iout, ctx.spec.fsw / 1e3,
                    spec_meta={"Vin": ctx.spec.Vin, "Vout": ctx.spec.Vout,
                               "Iout": ctx.spec.Iout, "fsw_khz": ctx.spec.fsw / 1e3,
                               "ripple_ratio": ctx.spec.ripple_ratio,
                               "Vripple": ctx.spec.Vripple},
                    best_config=ctx.artifacts.get("components", {}),
                    outcome=ctx.artifacts.get("thermal", {}),
                )
            except Exception:
                pass  # memory is best-effort; never fail the run on it
        return {"final": final, "done": True}

    g = StateGraph(AgentState)
    g.add_node("reason", reason)
    g.add_node("act", act)
    g.add_node("finalize", finalize)
    g.set_entry_point("reason")
    g.add_conditional_edges("reason", route, {"act": "act", "finalize": "finalize"})
    g.add_edge("act", "reason")
    g.add_edge("finalize", END)
    return g.compile()




def _call_openrouter(model: str, state: AgentState) -> dict:
    """One OpenRouter call (OpenAI-compatible, function calling).

    Model examples: deepseek/deepseek-chat-v3.1:free, deepseek/deepseek-r1,
    openai/gpt-4o-mini. API key from OPENROUTER_API_KEY."""
    import urllib.request

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise OrchestratorError("OPENROUTER_API_KEY not set in environment")

    tools = [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOL_SCHEMAS
    ]
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Task spec: {json.dumps(state['task'])}"}]
    for m in state["transcript"]:
        messages.append({"role": "user",
                         "content": f"{m.get('tool', m['role'])}: {json.dumps(m.get('result', ''))[:2000]}"})

    import urllib.request

    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": messages,
            "tools": tools,
        }).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read())

    msg = body["choices"][0]["message"]
    out: dict[str, Any] = {"tool_calls": [], "done": False, "final": None}
    for tc in msg.get("tool_calls") or []:
        fn = tc["function"]
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        out["tool_calls"].append({"name": fn["name"], "args": args})
    if not out["tool_calls"] and msg.get("content"):
        out["done"] = True
        out["final"] = {"summary": msg["content"]}
    return out


def run_agent(
    task_spec: dict,
    run_dir: str | Path,
    model: str = "deepseek/deepseek-v4-flash-0731",
    provider: str = "openrouter",
    max_steps: int = 12,
    mock_responses: list[dict] | None = None,
) -> dict:
    """Entry point: one orchestrated design run. Returns the final dict."""
    config = AgentConfig(run_dir=Path(run_dir), model=model, provider=provider,
                         max_steps=max_steps)
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(run_dir=Path(run_dir), library=load_library_or_raise())
    from pyspice_openfoam_agent.ui.run_state import RunState

    rs = RunState(run_dir)
    rs.init(task_spec, max_steps)
    graph = make_graph(config, ctx, mock_responses=mock_responses, run_state=rs)
    state = {
        "task": task_spec,
        "transcript": [],
        "tool_calls": [],
        "step": 0,
        "done": False,
        "final": None,
        "error": None,
    }
    result = graph.invoke(state, config={"recursion_limit": max_steps * 2 + 5})
    final = result.get("final") or {"error": result.get("error", "no final state")}
    rs.finish(final, error=result.get("error"))
    return final


def load_library_or_raise() -> Library:
    from pyspice_openfoam_agent.library.loader import load_library

    return load_library()