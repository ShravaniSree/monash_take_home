from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ap_agent.domain.engine import reconcile, role_for_amount
from ap_agent.domain.models import MatchType, Recommendation, ToolError, ToolErrorCode, VendorRecord


def _vendor() -> VendorRecord:
    return VendorRecord(
        vendor_id="V-100",
        legal_name="Acme Supplies Pty Ltd",
        status="ACTIVE",
        country="AU",
        payment_country="AU",
        account_last4="4412",
        domains=["acme.example"],
        created_at=datetime(2024, 1, 15, tzinfo=timezone.utc),
        updated_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


def _citations() -> dict[str, str]:
    return {f"FIN-POL-{index:03d}": f"FIN-POL-{index:03d}#section#0" for index in (1, 2, 3, 4, 5, 9)}


def test_role_for_amount_uses_current_limits():
    assert role_for_amount(Decimal("8000")) == "COST_CENTRE_MANAGER"
    assert role_for_amount(Decimal("10001")) == "DEPARTMENT_DIRECTOR"
    assert role_for_amount(Decimal("12500")) == "DEPARTMENT_DIRECTOR"


def test_exact_duplicate_rejects_without_payment():
    from ap_agent.domain.models import InvoiceHistoryMatch

    result = reconcile(
        invoice_ref="INV-2002",
        vendor_name="Acme Supplies Pty Ltd",
        amount=Decimal("4000.00"),
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        requester_id=None,
        receipter_id=None,
        vendor=_vendor(),
        vendor_error=None,
        purchase_order=None,
        po_error=None,
        history=[
            InvoiceHistoryMatch(
                record_id="HIST-88",
                invoice_ref="INV-2002",
                status="PAID",
                match_type=MatchType.EXACT,
                currency="AUD",
                gross_amount=Decimal("4000.00"),
            )
        ],
        indicators=[],
        citations=_citations(),
        aud_rate=Decimal("1"),
    )
    assert result.recommendation is Recommendation.REJECT_DUPLICATE
    assert result.next_action.value == "STOP_NO_PAYMENT"


def test_po_timeout_holds_and_does_not_approve():
    result = reconcile(
        invoice_ref="INV-1001",
        vendor_name="Acme Supplies Pty Ltd",
        amount=Decimal("12500.00"),
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        requester_id="U-REQ",
        receipter_id="U-REC",
        vendor=_vendor(),
        vendor_error=None,
        purchase_order=None,
        po_error=ToolError(code=ToolErrorCode.TIMEOUT, message="timeout"),
        history=[],
        indicators=[],
        citations=_citations(),
        aud_rate=Decimal("1"),
    )
    assert result.recommendation is Recommendation.HOLD_FOR_INFORMATION
    assert result.state.value == "COMPLETED"
    assert result.required_roles == []


def test_line_tolerance_uses_the_lower_cap():
    from ap_agent.domain.engine import reconcile
    from ap_agent.domain.models import GoodsReceipt
    from ap_agent.domain.models import PoLine
    from ap_agent.domain.models import PurchaseOrder

    def purchase_order(total: str, qty: str, price: str) -> PurchaseOrder:
        return PurchaseOrder(
            po_number="PO-1",
            vendor_id="V-100",
            currency="AUD",
            fx_permitted=False,
            freight_permitted=False,
            status="APPROVED",
            total=total,
            lines=[PoLine(line=1, description="Widgets", kind="goods", qty=qty, unit_price=price, line_total=total)],
            receipts=[GoodsReceipt(receipt_id="GR-1", line=1, qty=qty, received_on=date(2026, 11, 2))],
            approver_id="U-DIR-1",
        )

    over_absolute = reconcile(
        invoice_ref="INV-1",
        vendor_name="Acme Supplies Pty Ltd",
        amount=Decimal("10050.01"),
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        requester_id="U-REQ",
        receipter_id="U-REC",
        vendor=_vendor(),
        vendor_error=None,
        purchase_order=purchase_order("10000.00", "10", "1000.00"),
        po_error=None,
        history=[],
        indicators=[],
        citations=_citations(),
        aud_rate=Decimal("1"),
    )
    assert any(item.category.value == "PRICE_VARIANCE" for item in over_absolute.exceptions)

    over_percent = reconcile(
        invoice_ref="INV-2",
        vendor_name="Acme Supplies Pty Ltd",
        amount=Decimal("1011.00"),
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        requester_id="U-REQ",
        receipter_id="U-REC",
        vendor=_vendor(),
        vendor_error=None,
        purchase_order=purchase_order("1000.00", "10", "100.00"),
        po_error=None,
        history=[],
        indicators=[],
        citations=_citations(),
        aud_rate=Decimal("1"),
    )
    assert any(item.category.value == "PRICE_VARIANCE" for item in over_percent.exceptions)


def test_invoice_quantity_above_receipt_holds():
    from ap_agent.domain.models import GoodsReceipt
    from ap_agent.domain.models import InvoiceLine
    from ap_agent.domain.models import PoLine
    from ap_agent.domain.models import PurchaseOrder

    result = reconcile(
        invoice_ref="INV-1",
        vendor_name="Acme Supplies Pty Ltd",
        amount=Decimal("1000.00"),
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        requester_id="U-REQ",
        receipter_id="U-REC",
        vendor=_vendor(),
        vendor_error=None,
        purchase_order=PurchaseOrder(
            po_number="PO-1",
            vendor_id="V-100",
            currency="AUD",
            fx_permitted=False,
            freight_permitted=False,
            status="APPROVED",
            total="1000.00",
            lines=[PoLine(line=1, description="Widgets", kind="goods", qty="10", unit_price="100.00", line_total="1000.00")],
            receipts=[GoodsReceipt(receipt_id="GR-1", line=1, qty="10", received_on=date(2026, 11, 2))],
            approver_id="U-DIR-1",
        ),
        po_error=None,
        history=[],
        indicators=[],
        citations=_citations(),
        aud_rate=Decimal("1"),
        invoice_lines=[InvoiceLine(line=1, qty="11", line_total="1000.00")],
    )
    assert result.recommendation is Recommendation.HOLD_FOR_INFORMATION
    assert any(item.category.value == "QUANTITY_VARIANCE" for item in result.exceptions)


def test_recent_bank_change_holds_for_two_business_days():
    from ap_agent.domain.engine import bank_hold_active

    assert bank_hold_active(date(2026, 10, 30), date(2026, 11, 1)) is True
    assert bank_hold_active(date(2026, 10, 1), date(2026, 11, 1)) is False


def test_same_purchase_order_alone_is_not_a_fuzzy_duplicate():
    from ap_agent.systems.world import FinanceWorld

    world = FinanceWorld.from_tools(
        {
            "invoice_history": [
                {
                    "record_id": "HIST-1",
                    "vendor_id": "V-100",
                    "invoice_ref": "INV-OTHER",
                    "status": "POSTED",
                    "currency": "AUD",
                    "gross_amount": "100.00",
                    "invoice_date": "2026-11-01",
                    "po_number": "PO-9001",
                }
            ]
        }
    )
    matches = world.check_history(
        vendor_id="V-100",
        invoice_ref="INV-NEW",
        invoice_date=date(2026, 11, 1),
        currency="AUD",
        gross_amount=Decimal("9000.00"),
        po_number="PO-9001",
    )
    assert matches == []


def test_narrative_repairs_once_then_uses_the_template():
    from ap_agent.domain.narrative import resolve_narrative

    text, rejected = resolve_narrative(
        ["Recommendation REJECT_DUPLICATE is the outcome.", "Recommendation APPROVE_FOR_POSTING is unchanged."],
        recommendation="APPROVE_FOR_POSTING",
        citation_ids=["FIN-POL-002#2-tolerances#1"],
        allowed_numbers=set(),
    )
    assert rejected is False
    assert "APPROVE_FOR_POSTING" in text
    assert "REJECT_DUPLICATE" not in text

    text, rejected = resolve_narrative(
        ["Recommendation REJECT_DUPLICATE.", "The amount is 99999.99."],
        recommendation="APPROVE_FOR_POSTING",
        citation_ids=[],
        allowed_numbers=set(),
    )
    assert rejected is True
    assert "APPROVE_FOR_POSTING" in text
    assert "99999.99" not in text


def test_submit_requires_a_matching_snapshot():
    from ap_agent.domain.models import SubmitDecision
    from ap_agent.systems.world import FinanceWorld
    from ap_agent.systems.world import ToolFault

    world = FinanceWorld()
    decision = SubmitDecision(
        idempotency_key="abc",
        run_id="run",
        case_id="FIN-001",
        decision="POST",
        vendor_id="V-100",
        invoice_ref="INV-1001",
        currency="AUD",
        amount="12500.00",
        approval_id="appr",
    )
    with pytest.raises(ToolFault):
        world.submit(decision, approved=True, snapshot=None)
    posted = world.submit(
        decision,
        approved=True,
        snapshot={
            "recommendation": "APPROVE_FOR_POSTING",
            "vendor_id": "V-100",
            "invoice_ref": "INV-1001",
            "currency": "AUD",
            "amount": "12500.00",
        },
    )
    assert posted.replayed is False
    with pytest.raises(ToolFault):
        world.submit(decision, approved=False, snapshot={"recommendation": "APPROVE_FOR_POSTING"})


def test_transient_tool_is_retried_and_a_deadline_is_a_timeout():
    import time

    from ap_agent.application.evidence import call_tool
    from ap_agent.systems.world import ToolFault

    calls: list[dict] = []
    state = {"left": 1}

    def flaky() -> str:
        if state["left"]:
            state["left"] -= 1
            raise ToolFault(ToolErrorCode.TRANSIENT, "try again")
        return "ok"

    value, attempts, error = call_tool(calls, "get_purchase_order", {"po_number": "PO-1"}, flaky, timeout_s=1)
    assert value == "ok"
    assert attempts == 2
    assert error is None
    assert calls[0]["error"]["code"] == "TRANSIENT"
    assert calls[0]["duration_ms"] >= 0
    assert calls[0]["ts"]

    def slow() -> str:
        time.sleep(0.3)
        return "late"

    timed_out, timed_attempts, timed_error = call_tool([], "get_vendor_record", {}, slow, timeout_s=0.05)
    assert timed_out is None
    assert timed_attempts == 3
    assert timed_error is not None
    assert timed_error.code is ToolErrorCode.TIMEOUT
