"""Phase 3: multi-criteria topology synthesis (goals: not just voltage ratio).

Evaluates viable topologies against efficiency/stress/component-count
criteria, produces a ranked recommendation with rationale, and applies the
goals' ask policy: if candidates are close, present them to the user unless
an optimization policy decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyspice_openfoam_agent.design.object import Requirements


class TopologyError(ValueError):
    """No viable topology for the requirement."""


@dataclass
class TopologyCandidate:
    name: str
    score: float  # higher = better
    viable: bool
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    switch_count: int = 2
    est_efficiency: float = 0.0  # crude conduction-only estimate


def evaluate_topologies(req: Requirements) -> list[TopologyCandidate]:
    """Rank viable topologies for the requirement. Deterministic scoring."""
    ratio = req.Vout / req.Vin
    candidates: list[TopologyCandidate] = []

    # buck viable if stepping down
    if ratio < 1.0:
        p_cond = req.Iout ** 2 * 0.003  # assumed 3 mOhm total path
        eff = req.Vout * req.Iout / (req.Vout * req.Iout + p_cond)
        candidates.append(TopologyCandidate(
            name="buck", score=1.0 + (0.2 if ratio < 0.9 else 0.0),
            viable=True, switch_count=2, est_efficiency=eff,
            pros=["fewest components", "simple control", "well-understood thermal map"],
            cons=["no isolation", "limited to step-down"],
        ))
    # boost viable if stepping up
    if ratio > 1.0:
        p_cond = (req.Iout / (1 - 0.5833)) ** 2 * 0.004  # crude inductor-current stress
        eff = req.Vout * req.Iout / (req.Vout * req.Iout + p_cond)
        candidates.append(TopologyCandidate(
            name="boost", score=0.9, viable=True, switch_count=2, est_efficiency=eff,
            pros=["fewest components for step-up"],
            cons=["no isolation", "poor transient control", "output current limited"],
        ))
    # buck-boost viable always
    d = req.Vout / (req.Vin + req.Vout)
    p_cond = req.Iout ** 2 * 0.005
    eff = req.Vout * req.Iout / (req.Vout * req.Iout + p_cond)
    candidates.append(TopologyCandidate(
        name="buck_boost", score=0.75, viable=True, switch_count=4,
        est_efficiency=eff,
        pros=["any Vin/Vout ratio", "handles wide input ranges"],
        cons=["4 switches: higher cost + gate-drive complexity",
              "lower efficiency (more conduction path)"],
    ))

    if not candidates:
        raise TopologyError(f"no viable topology for Vin={req.Vin} Vout={req.Vout}")

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def recommend_topology(req: Requirements, policy: str | None = None) -> tuple[str, list[TopologyCandidate]]:
    """Pick the topology per the goals' policy.

    policy: an optimization policy string ('lowest_cost', 'max_efficiency').
    Returns (chosen_name, all_candidates). If the top two are within 0.1
    score and no policy exists, the recommendation is provisional — the
    caller (orchestrator/HITL) should present both to the user.
    """
    cands = evaluate_topologies(req)
    viable = [c for c in cands if c.viable]
    if not viable:
        raise TopologyError("no viable topologies")
    best = viable[0]
    if policy == "max_efficiency" and len(viable) > 1:
        best = max(viable, key=lambda c: c.est_efficiency)
    return best.name, viable


def is_ambiguous(viable: list[TopologyCandidate]) -> bool:
    """Goals policy: top-two within 0.1 score = present both to the user."""
    return len(viable) >= 2 and (viable[0].score - viable[1].score) < 0.1