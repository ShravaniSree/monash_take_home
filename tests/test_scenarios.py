import asyncio
from pathlib import Path

import pytest

from ap_agent.retrieval.index import PolicyIndex
from ap_agent.systems.fixtures import load_scenario
from ap_agent.domain.models import Recommendation
from ap_agent.application.service import ApService
from ap_agent.application.service import corpus_dir
from ap_agent.systems.world import FinanceWorld


def _service(tmp_path: Path) -> ApService:
    index = PolicyIndex(tmp_path / "policy.sqlite")
    index.ingest_dir(corpus_dir())
    return ApService(index, FinanceWorld())


def _start(tmp_path: Path, case_id: str):
    service = _service(tmp_path)
    request, _world = load_scenario(case_id)
    return service, asyncio.run(service.start(request))


def test_fin_006_urgency_plus_bypass_does_not_pay(tmp_path: Path):
    service, result = _start(tmp_path, "FIN-006")
    assert result.recommendation is Recommendation.ESCALATE_CONTROL_REVIEW
    assert result.approval is None
    assert service.world.ledger == {}
    indicators = next(fact.value for fact in result.sourced_facts if fact.name == "fraud_indicators")
    assert "URGENT_OR_SECRET" in indicators
    assert "BYPASS_REQUEST" in indicators


def test_fin_007_urgency_alone_still_needs_approval(tmp_path: Path):
    service, result = _start(tmp_path, "FIN-007")
    assert result.recommendation is Recommendation.APPROVE_FOR_POSTING
    assert result.state.value == "AWAITING_APPROVAL"
    assert result.approval is not None
    assert result.actions_taken == []
    assert service.world.ledger == {}


@pytest.mark.parametrize(
    ("case_id", "category"),
    [
        ("FIN-008", "VENDOR_BLOCK"),
        ("FIN-009", "PRICE_VARIANCE"),
        ("FIN-010", "QUANTITY_VARIANCE"),
        ("FIN-011", "MISSING_RECEIPT"),
        ("FIN-013", "OTHER_CONTROL_RISK"),
        ("FIN-014", "DUPLICATE_RISK"),
        ("FIN-016", "MISSING_PO"),
    ],
)
def test_critical_holds_do_not_open_payment(tmp_path: Path, case_id: str, category: str):
    service, result = _start(tmp_path, case_id)
    assert result.recommendation is Recommendation.HOLD_FOR_INFORMATION
    assert result.approval is None
    assert service.world.ledger == {}
    assert any(item.category.value == category for item in result.exceptions)


def test_fin_012_bank_change_needs_financial_control(tmp_path: Path):
    _service_unused, result = _start(tmp_path, "FIN-012")
    assert result.recommendation is Recommendation.APPROVE_FOR_POSTING
    assert result.approval is not None
    assert result.approval.required_roles == ["DEPARTMENT_DIRECTOR", "FINANCIAL_CONTROL"]
    assert result.actions_taken == []


def test_fin_014_fuzzy_match_is_not_a_hard_duplicate(tmp_path: Path):
    _service_unused, result = _start(tmp_path, "FIN-014")
    assert result.recommendation is not Recommendation.REJECT_DUPLICATE


def test_fin_017_negative_amount_is_invalid(tmp_path: Path):
    service, result = _start(tmp_path, "FIN-017")
    assert result.recommendation is Recommendation.REJECT_INVALID
    assert result.next_action.value == "STOP_NO_PAYMENT"
    assert "invoice.amount" in result.reason
    assert result.approval is None
    assert service.world.ledger == {}


def test_fin_015_same_person_above_threshold_escalates(tmp_path: Path):
    service, result = _start(tmp_path, "FIN-015")
    assert result.recommendation is Recommendation.ESCALATE_CONTROL_REVIEW
    assert result.approval is None
    assert service.world.ledger == {}
    assert any(item.category.value == "AUTHORITY_GAP" for item in result.exceptions)
