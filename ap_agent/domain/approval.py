"""Decide whether a human vote may post, wait, or close the case.

Financial Control is a co-approver. That role does not by itself authorise
a payment, and a vote from someone under the monetary limit does not close
the case. An explicit rejection does.
"""

from __future__ import annotations

from decimal import Decimal

from ap_agent.domain.engine import role_can_approve
from ap_agent.domain.models import ApprovalDecision
from ap_agent.domain.models import NextAction
from ap_agent.domain.models import RunResult
from ap_agent.domain.models import RunState


class ApprovalOutcome:
    """Result of applying one human vote to a waiting run."""

    def __init__(self, result: RunResult, votes: list[dict], *, post: bool, terminal: bool) -> None:
        """Store the updated snapshot and whether the ledger may be called.

        Args:
            result: Run snapshot after the vote.
            votes: Accepted and rejected votes so far, including this one.
            post: True when every required role has now approved.
            terminal: True when the run must leave ``AWAITING_APPROVAL``.
        """
        self.result = result
        self.votes = votes
        self.post = post
        self.terminal = terminal


def _same_vote(vote: dict, decision: ApprovalDecision) -> bool:
    """Return True when this vote repeats an earlier one from the same person."""
    return (
        vote.get("approver_id") == decision.approver_id
        and vote.get("role") == decision.role
        and vote.get("action") == decision.action.value
    )


def apply_approval(
    result: RunResult,
    decision: ApprovalDecision,
    votes: list[dict],
    amount_aud: Decimal,
) -> ApprovalOutcome:
    """Apply one approval or rejection without posting.

    Args:
        result: Snapshot produced by the rules engine. It is copied.
        decision: Human vote.
        votes: Votes already stored on the run.
        amount_aud: Gross commitment in AUD from the engine.

    Returns:
        The next snapshot. ``post`` is true only when the monetary role
        and, where required, Financial Control have both approved.
    """
    current = result.model_copy(deep=True)
    recorded = [dict(vote) for vote in votes]
    if current.approval is None:
        current.state = RunState.COMPLETED
        return ApprovalOutcome(current, recorded, post=False, terminal=True)
    if any(_same_vote(vote, decision) for vote in recorded):
        return ApprovalOutcome(current, recorded, post=False, terminal=False)
    required = list(current.approval.required_roles)
    if decision.action.value == "REJECTED":
        recorded.append(
            {
                "approver_id": decision.approver_id,
                "role": decision.role,
                "action": decision.action.value,
                "accepted": False,
                "reason": "REJECTED",
            }
        )
        current.approval.status = "REJECTED"
        current.approval.reason = "REJECTED"
        current.state = RunState.COMPLETED
        current.next_action = NextAction.STOP_NO_PAYMENT
        return ApprovalOutcome(current, recorded, post=False, terminal=True)

    accepted = False
    reason = None
    if decision.role == "FINANCIAL_CONTROL":
        if "FINANCIAL_CONTROL" in required:
            accepted = True
        else:
            reason = "AUTHORITY_GAP"
    elif role_can_approve(decision.role, amount_aud):
        accepted = True
    else:
        reason = "AUTHORITY_GAP"
    recorded.append(
        {
            "approver_id": decision.approver_id,
            "role": decision.role,
            "action": decision.action.value,
            "accepted": accepted,
            "reason": reason,
        }
    )
    accepted_votes = [vote for vote in recorded if vote["accepted"]]
    current.approval.received_roles = sorted({str(vote["role"]) for vote in accepted_votes})
    if not accepted:
        current.approval.status = "PENDING"
        current.approval.reason = reason
        current.state = RunState.AWAITING_APPROVAL
        return ApprovalOutcome(current, recorded, post=False, terminal=False)

    has_monetary = any(
        vote["role"] != "FINANCIAL_CONTROL" and role_can_approve(str(vote["role"]), amount_aud)
        for vote in accepted_votes
    )
    has_control = any(vote["role"] == "FINANCIAL_CONTROL" for vote in accepted_votes)
    needs_control = "FINANCIAL_CONTROL" in required
    if has_monetary and (not needs_control or has_control):
        current.approval.reason = None
        return ApprovalOutcome(current, recorded, post=True, terminal=True)
    current.approval.status = "PENDING"
    current.approval.reason = None
    current.state = RunState.AWAITING_APPROVAL
    return ApprovalOutcome(current, recorded, post=False, terminal=False)
