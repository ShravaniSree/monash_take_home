import asyncio
from pathlib import Path

from ap_agent.retrieval.index import PolicyIndex
from ap_agent.systems.fixtures import load_scenario
from ap_agent.domain.models import ApprovalDecision
from ap_agent.domain.models import Recommendation
from ap_agent.application.app import APP_NAME
from ap_agent.application.app import USER_ID
from ap_agent.application.service import ApService
from ap_agent.application.service import corpus_dir
from ap_agent.systems.world import FinanceWorld


def _index(tmp_path: Path) -> PolicyIndex:
    index = PolicyIndex(tmp_path / "policy.sqlite")
    assert index.ingest_dir(corpus_dir()) == 15
    return index


def _service(tmp_path: Path) -> ApService:
    return ApService(_index(tmp_path), FinanceWorld())


def _run(coro):
    return asyncio.run(coro)


def test_fin_001_stops_for_approval_then_posts_once(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-001")

    async def scenario():
        waiting = await service.start(request)
        assert waiting.recommendation is Recommendation.APPROVE_FOR_POSTING
        assert waiting.reason == "required checks passed"
        assert waiting.state.value == "AWAITING_APPROVAL"
        assert waiting.approval is not None
        assert waiting.approval.required_roles == ["DEPARTMENT_DIRECTOR"]
        assert waiting.actions_taken == []
        assert any(item.rule_id == "tolerance" for item in waiting.policy_findings)
        retrieved = [
            chunk["chunk_id"]
            for call in waiting.tool_calls
            if call.tool == "retrieve_finance_documents" and isinstance(call.response, list)
            for chunk in call.response
        ]
        for finding in waiting.policy_findings:
            assert finding.chunk_id in retrieved
        posted = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        replayed = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        return posted, replayed

    posted, replayed = _run(scenario())
    assert posted.state.value == "COMPLETED"
    assert len(posted.actions_taken) == 1
    assert len(service.world.ledger) == 1
    assert replayed.actions_taken[0].ledger_ref == posted.actions_taken[0].ledger_ref
    assert service.world.ledger[posted.actions_taken[0].idempotency_key].ledger_ref == posted.actions_taken[0].ledger_ref


def test_fin_002_does_not_propose_payment(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-002")

    result = _run(service.start(request))
    assert result.recommendation is Recommendation.REJECT_DUPLICATE
    assert result.next_action.value == "STOP_NO_PAYMENT"
    assert result.approval is None
    assert result.actions_taken == []
    assert service.world.ledger == {}


def test_fin_003_poisoned_attachment_does_not_post(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-003")

    result = _run(service.start(request))
    assert result.recommendation is Recommendation.ESCALATE_CONTROL_REVIEW
    assert result.approval is None
    assert service.world.ledger == {}
    indicators = next(fact.value for fact in result.sourced_facts if fact.name == "fraud_indicators")
    assert "BYPASS_REQUEST" in indicators
    assert "BANK_CHANGE_LANGUAGE" in indicators


def test_fin_004_po_timeout_does_not_ask_for_payment(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-004")

    result = _run(service.start(request))
    assert result.recommendation is Recommendation.HOLD_FOR_INFORMATION
    assert "purchase order unavailable (TIMEOUT)" in result.reason
    assert result.approval is None
    assert service.world.po_failures_remaining == 0
    assert service.world.ledger == {}


def test_fin_005_duplicate_approval_posts_once(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-005")

    async def scenario():
        waiting = await service.start(request)
        posted = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        replayed = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        return posted, replayed

    posted, replayed = _run(scenario())
    assert len(service.world.ledger) == 1
    assert replayed.actions_taken[0].ledger_ref == posted.actions_taken[0].ledger_ref


def test_rejection_cannot_be_followed_by_a_post(tmp_path: Path):
    from ap_agent.application.service import DecisionConflict

    service = _service(tmp_path)
    request, _world = load_scenario("FIN-001")

    async def scenario():
        waiting = await service.start(request)
        rejected = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="REJECTED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        try:
            await service.decide(
                waiting.run_id,
                ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
            )
        except DecisionConflict as exc:
            return rejected, exc
        raise AssertionError("a later approval was accepted")

    rejected, conflict = _run(scenario())
    assert rejected.state.value == "COMPLETED"
    assert rejected.approval is not None
    assert rejected.approval.status == "REJECTED"
    assert rejected.actions_taken == []
    assert conflict.result.actions_taken == []
    assert service.world.ledger == {}


def test_under_limit_approver_does_not_close_the_run(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-001")

    async def scenario():
        waiting = await service.start(request)
        held = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-CCM", role="COST_CENTRE_MANAGER"),
        )
        posted = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        return held, posted

    held, posted = _run(scenario())
    assert held.state.value == "AWAITING_APPROVAL"
    assert held.approval is not None
    assert held.approval.reason == "AUTHORITY_GAP"
    assert held.actions_taken == []
    assert posted.state.value == "COMPLETED"
    assert len(posted.actions_taken) == 1


def test_bank_change_posts_only_after_both_roles(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-012")

    async def scenario():
        waiting = await service.start(request)
        monetary = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        posted = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-FC-1", role="FINANCIAL_CONTROL"),
        )
        return waiting, monetary, posted

    waiting, monetary, posted = _run(scenario())
    assert waiting.approval is not None
    assert waiting.approval.required_roles == ["DEPARTMENT_DIRECTOR", "FINANCIAL_CONTROL"]
    assert monetary.state.value == "AWAITING_APPROVAL"
    assert monetary.actions_taken == []
    assert posted.state.value == "COMPLETED"
    assert len(posted.actions_taken) == 1
    assert len(service.world.ledger) == 1


def test_restart_replays_the_same_post(tmp_path: Path):
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    from ap_agent.systems.store import AuditStore

    index = _index(tmp_path)
    store = AuditStore(tmp_path / "ap_agent.sqlite")
    sessions = SqliteSessionService(str(tmp_path / "adk_sessions.sqlite"))
    service = ApService(index, FinanceWorld(), sessions, store)
    request, _world = load_scenario("FIN-001")

    async def scenario():
        waiting = await service.start(request)
        posted = await service.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        restarted = ApService(
            index,
            FinanceWorld(),
            SqliteSessionService(str(tmp_path / "adk_sessions.sqlite")),
            AuditStore(tmp_path / "ap_agent.sqlite"),
        )
        replayed = await restarted.decide(
            waiting.run_id,
            ApprovalDecision(action="APPROVED", approver_id="U-DIR-1", role="DEPARTMENT_DIRECTOR"),
        )
        return posted, replayed, restarted

    posted, replayed, restarted = _run(scenario())
    assert replayed.actions_taken[0].ledger_ref == posted.actions_taken[0].ledger_ref
    assert restarted.store.count_decisions() == 1


def test_account_number_in_notes_is_masked(tmp_path: Path):
    service = _service(tmp_path)
    request, _world = load_scenario("FIN-001")
    request = request.model_copy(update={"notes": "pay account 123456789 today"})

    async def scenario():
        result = await service.start(request)
        session = await service.sessions.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=result.run_id)
        return result, session

    result, session = _run(scenario())
    notes = session.state["request"]["notes"]
    assert "123456789" not in notes
    assert "****6789" in notes
    assert "123456789" not in result.model_dump_json()
