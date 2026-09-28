"""Reviewer agent: a second model reviews a proposed commit's diff before it
reaches the human approval gate, catching issues before you're even asked to
approve them. Uses a different model than the one that authored the change
where possible -- a model reviewing its own output has known blind spots
about its own mistakes. See PLAN.md's "Roadmap v2" for the design rationale.
"""

from __future__ import annotations

import litellm

REVIEW_SYSTEM_PROMPT = (
    "You are reviewing a proposed code change before it is committed. You will be given "
    "the original task and the full diff. Check for: correctness (does it actually do what "
    "the task asked), test coverage (are the changes tested), obvious bugs, security issues, "
    "and whether anything looks unrelated to the task.\n\n"
    "Reply with your verdict on the first line, exactly 'APPROVE' or 'REQUEST_CHANGES', then "
    "a short explanation on the following lines. If you REQUEST_CHANGES, be specific about "
    "what to fix -- your explanation is sent directly back to the model that wrote the change."
)


def pick_reviewer_model(model_chain: list[str], author_model: str) -> str:
    """Prefer a model different from the one that wrote the change; fall back
    to the same model (self-review) only if nothing else is in the chain."""
    for m in model_chain:
        if m != author_model:
            return m
    return author_model


def review_diff(*, task: str, diff: str, author_model: str, reviewer_model: str) -> tuple[bool, str]:
    """Returns (approved, feedback_text). Never raises -- a reviewer failure
    (network error, empty response) fails open (approved=True) with a note,
    since the human approval gate downstream is still the real safety net."""
    if not diff.strip():
        return True, "(nothing to review -- empty diff)"

    self_review_note = (
        " (note: you are reviewing a change written by yourself -- no second model was "
        "available in the configured chain; be appropriately skeptical of your own work)"
        if reviewer_model == author_model
        else ""
    )
    prompt = f"Task: {task}\n\nProposed diff:\n```diff\n{diff}\n```\n\nReview this change.{self_review_note}"

    try:
        response = litellm.completion(
            model=reviewer_model,
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
    except Exception as e:  # noqa: BLE001 -- reviewer failing must never block the whole task
        return True, f"(reviewer call failed, skipping review this round: {type(e).__name__}: {e})"

    if not response.choices:
        return True, "(reviewer returned no response, skipping review this round)"

    text = response.choices[0].message.content or ""
    first_line = text.strip().splitlines()[0].strip().upper() if text.strip() else ""
    approved = first_line.startswith("APPROVE")
    return approved, text
