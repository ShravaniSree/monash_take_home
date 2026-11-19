from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ap_agent.domain.models import (
    ProcessingRequest,
    RetrievedChunk,
    RunResult,
    RunState,
    SubmitDecision,
    VendorRecord,
)


def test_processing_request_keeps_money_as_decimal():
    request = ProcessingRequest(
        case_id="FIN-001",
        invoice_ref="INV-1001",
        vendor_id="V-100",
        vendor_name="Acme Supplies Pty Ltd",
        amount="12500.00",
        currency="AUD",
        invoice_date=date(2026, 11, 1),
        po_number="PO-9001",
    )
    assert request.amount == Decimal("12500.00")
    assert request.model_dump(mode="json")["amount"] == "12500.00"


def test_vendor_record_rejects_full_account_number():
    payload = {
        "vendor_id": "V-100",
        "legal_name": "Acme Supplies Pty Ltd",
        "status": "ACTIVE",
        "country": "AU",
        "payment_country": "AU",
        "account_last4": "44121234",
        "domains": ["acme.example"],
        "created_at": datetime(2024, 1, 15, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
    }
    with pytest.raises(ValidationError):
        VendorRecord.model_validate(payload)


def test_run_result_round_trip():
    result = RunResult.model_validate(
        {
            "run_id": "run_01H",
            "case_id": "FIN-001",
            "state": "AWAITING_APPROVAL",
            "recommendation": "APPROVE_FOR_POSTING",
            "sourced_facts": [],
            "calculations": [],
            "inferences": [],
            "unknowns": [],
            "policy_findings": [],
            "exceptions": [],
            "confidence": {"level": "high", "reasons": ["all required tools returned"]},
            "next_action": "REQUEST_APPROVAL",
            "approval": {
                "approval_id": "appr_01H",
                "required_roles": ["DEPARTMENT_DIRECTOR"],
                "status": "PENDING",
            },
            "actions_taken": [],
        }
    )
    assert result.state is RunState.AWAITING_APPROVAL
    assert result.approval is not None
    assert result.approval.required_roles == ["DEPARTMENT_DIRECTOR"]


def test_retrieved_chunk_requires_trust_flags():
    chunk = RetrievedChunk.model_validate(
        {
            "chunk_id": "FIN-POL-003-OLD#general-rules#0",
            "document_id": "FIN-POL-003-OLD",
            "title": "Delegated Financial Authority Matrix",
            "doc_type": "policy",
            "version": "1.0",
            "section": "Superseded",
            "status": "superseded",
            "trust": "trusted",
            "stale": True,
            "classification": "internal",
            "score": 0.2,
            "text": "historical",
        }
    )
    assert chunk.stale is True


def test_submit_decision_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        SubmitDecision.model_validate(
            {
                "idempotency_key": "abc",
                "run_id": "run_01H",
                "case_id": "FIN-001",
                "decision": "POST",
                "vendor_id": "V-100",
                "invoice_ref": "INV-1001",
                "currency": "AUD",
                "amount": "12500.00",
                "approval_id": "appr_01H",
                "skip_approval": True,
            }
        )
