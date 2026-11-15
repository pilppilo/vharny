"""Diagnostic-only scripted policies retained for reproducible checkpoints."""

from __future__ import annotations

from .kernel import ActionChoice, EvaluationChoice


def frozenlake_diagnostic_checkpoint_results() -> (
    tuple[ActionChoice | EvaluationChoice, ...]
):
    """Return the fixed FrozenLake route used only for checkpoint diagnostics.

    This is test/evidence policy, not Gymnasium adapter behavior and not evidence
    of domain-neutral model reasoning.
    """
    steps = tuple(
        ActionChoice("step", "move toward the goal", {"action": action})
        for action in (1, 1, 2, 1, 2, 2)
    )
    return (
        ActionChoice("reset", "official reset"),
        *steps,
        EvaluationChoice("report native FrozenLake goal"),
    )
