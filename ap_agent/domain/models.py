"""Typed contracts for cases, retrieval, tools, and run results.

Money is decimal. Model output is not a source of amounts.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator

Money = Annotated[
    Decimal,
    PlainSerializer(lambda value: format(value, "f"), return_type=str),
]


class Recommendation(str, Enum):
    APPROVE_FOR_POSTING = "APPROVE_FOR_POSTING"
    HOLD_FOR_INFORMATION = "HOLD_FOR_INFORMATION"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"
    REJECT_INVALID = "REJECT_INVALID"
    ESCALATE_CONTROL_REVIEW = "ESCALATE_CONTROL_REVIEW"


class RunState(str, Enum):
    RECEIVED = "RECEIVED"
    RETRIEVING = "RETRIEVING"
    GATHERING_EVIDENCE = "GATHERING_EVIDENCE"
    RECONCILING = "RECONCILING"
    RECOMMENDING = "RECOMMENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUBMITTING = "SUBMITTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class NextAction(str, Enum):
    REQUEST_APPROVAL = "REQUEST_APPROVAL"
    STOP_NO_PAYMENT = "STOP_NO_PAYMENT"
    ESCALATE = "ESCALATE"
    RETRY_EVIDENCE = "RETRY_EVIDENCE"


class ToolErrorCode(str, Enum):
    TIMEOUT = "TIMEOUT"
    TRANSIENT = "TRANSIENT"
    NOT_FOUND = "NOT_FOUND"
    SCHEMA = "SCHEMA"
    DENIED = "DENIED"
    CONFLICT = "CONFLICT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


class DocType(str, Enum):
    POLICY = "policy"
    ADVERSARIAL = "adversarial"
    IRRELEVANT = "irrelevant"


class Trust(str, Enum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


class VendorStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    DORMANT = "DORMANT"
    SANCTIONS_REVIEW = "SANCTIONS_REVIEW"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"


class HistoryStatus(str, Enum):
    PAID = "PAID"
    POSTED = "POSTED"
    HELD = "HELD"
    REJECTED = "REJECTED"


class MatchType(str, Enum):
    EXACT = "EXACT"
    FUZZY = "FUZZY"


class LedgerDecision(str, Enum):
    POST = "POST"
    HOLD = "HOLD"
    REJECT = "REJECT"


class ApprovalAction(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ExceptionCategory(str, Enum):
    MISSING_PO = "MISSING_PO"
    MISSING_RECEIPT = "MISSING_RECEIPT"
    PRICE_VARIANCE = "PRICE_VARIANCE"
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    DUPLICATE_RISK = "DUPLICATE_RISK"
    VENDOR_BLOCK = "VENDOR_BLOCK"
    BANK_CHANGE = "BANK_CHANGE"
    AUTHORITY_GAP = "AUTHORITY_GAP"
    TAX_QUERY = "TAX_QUERY"
    OTHER_CONTROL_RISK = "OTHER_CONTROL_RISK"


class NonPoCategory(str, Enum):
    STATUTORY_CHARGE = "STATUTORY_CHARGE"
    APPROVED_UTILITY = "APPROVED_UTILITY"
    PROPERTY_LEASE = "PROPERTY_LEASE"
    PROFESSIONAL_MEMBERSHIP = "PROFESSIONAL_MEMBERSHIP"
    GENUINE_EMERGENCY = "GENUINE_EMERGENCY"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Attachment(_Model):
    name: str
    text: str


class InvoiceLine(_Model):
    """One invoiced line. When this is absent, quantity is an unknown."""

    line: int
    qty: Money
    line_total: Money


class NonPoJustification(_Model):
    """Requester-supplied non-PO claim; it is not an approval record."""

    category: NonPoCategory
    reason: str = Field(min_length=5, max_length=2000)
    cost_centre: str = Field(min_length=1, max_length=100)
    receipt_evidence_ref: str = Field(min_length=1, max_length=500)
    retrospective_review_ref: str | None = Field(default=None, max_length=500)


class ProcessingRequest(_Model):
    case_id: str
    invoice_ref: str
    vendor_id: str
    vendor_name: str
    amount: Money
    currency: str = Field(min_length=3, max_length=3)
    invoice_date: date | None = None
    po_number: str | None = None
    non_po_justification: NonPoJustification | None = None
    notes: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    requester_id: str | None = None
    receipter_id: str | None = None
    lines: list[InvoiceLine] = Field(default_factory=list)
    freight: Money | None = None
    service_confirmed: bool = False

    @model_validator(mode="after")
    def justification_requires_missing_po(self) -> "ProcessingRequest":
        if self.po_number and self.non_po_justification is not None:
            raise ValueError("non_po_justification is only valid when po_number is absent")
        return self


class ToolError(_Model):
    code: ToolErrorCode
    message: str


class ToolEnvelope(_Model):
    ok: bool
    error: ToolError | None = None
    duration_ms: int = Field(ge=0)
    attempt: int = Field(ge=1)


class RetrieveQuery(_Model):
    query: str
    topics: list[str] = Field(default_factory=list)
    role: str = "ap_processor"
    top_k: int = Field(default=6, ge=1, le=20)


class RetrievedChunk(_Model):
    chunk_id: str
    document_id: str
    title: str
    doc_type: DocType
    version: str
    section: str
    status: str
    trust: Trust
    stale: bool
    classification: str
    score: float
    text: str


class VendorQuery(_Model):
    vendor_id: str


class VendorRecord(_Model):
    vendor_id: str
    legal_name: str
    status: VendorStatus
    country: str
    payment_country: str
    account_last4: str = Field(min_length=4, max_length=4)
    domains: list[str]
    created_at: datetime
    bank_change_verified_at: datetime | None = None
    risk_flags: list[str] = Field(default_factory=list)
    updated_at: datetime


class PurchaseOrderQuery(_Model):
    po_number: str


class PoLine(_Model):
    line: int
    description: str
    kind: Literal["goods", "services"]
    qty: Money
    unit_price: Money
    line_total: Money


class GoodsReceipt(_Model):
    receipt_id: str
    line: int
    qty: Money
    received_on: date


class PurchaseOrder(_Model):
    po_number: str
    vendor_id: str
    currency: str = Field(min_length=3, max_length=3)
    fx_permitted: bool
    freight_permitted: bool
    status: str
    total: Money
    lines: list[PoLine]
    receipts: list[GoodsReceipt]
    approver_id: str | None = None


class InvoiceHistoryQuery(_Model):
    vendor_id: str
    invoice_ref: str
    invoice_date: date | None = None
    currency: str = Field(min_length=3, max_length=3)
    gross_amount: Money
    po_number: str | None = None
    attachment_sha256: str | None = None


class InvoiceHistoryMatch(_Model):
    record_id: str
    invoice_ref: str
    status: HistoryStatus
    match_type: MatchType
    currency: str
    gross_amount: Money
    invoice_date: date | None = None


class SubmitDecision(_Model):
    idempotency_key: str
    run_id: str
    case_id: str
    decision: LedgerDecision
    vendor_id: str
    invoice_ref: str
    currency: str = Field(min_length=3, max_length=3)
    amount: Money
    approval_id: str


class SubmitResult(_Model):
    ledger_ref: str
    decision: LedgerDecision
    replayed: bool


class FactSource(_Model):
    kind: Literal["tool", "chunk"]
    tool: str | None = None
    record_id: str | None = None
    chunk_id: str | None = None
    document_id: str | None = None
    version: str | None = None


class SourcedFact(_Model):
    name: str
    value: Any
    source: FactSource


class Calculation(_Model):
    name: str
    inputs: dict[str, Any]
    formula: str
    result: dict[str, Any]
    rounding: str


class PolicyFinding(_Model):
    rule_id: str
    result: Literal["pass", "fail", "not_applicable"]
    chunk_id: str


class CaseException(_Model):
    category: ExceptionCategory
    failed_rule: str
    expected: str
    observed: str
    chunk_id: str | None = None
    owner: str
    next_review_date: date | None = None


class Confidence(_Model):
    level: Literal["high", "medium", "low"]
    reasons: list[str]


class ApprovalView(_Model):
    approval_id: str
    required_roles: list[str]
    status: Literal["PENDING", "APPROVED", "REJECTED"]
    received_roles: list[str] = Field(default_factory=list)
    reason: str | None = None


class ToolCallRecord(_Model):
    tool: str
    attempt: int = Field(ge=1)
    request: dict[str, Any]
    response: Any = None
    error: ToolError | None = None
    ts: str = ""
    duration_ms: int = Field(default=0, ge=0)


class AuditEvent(_Model):
    """One persisted workflow event. Detail is redacted before it is stored."""

    ts: str
    run_id: str
    type: str
    outcome: str
    duration_ms: int = Field(default=0, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class ActionTaken(_Model):
    tool: str
    idempotency_key: str
    ledger_ref: str
    decision: LedgerDecision


class RunResult(_Model):
    run_id: str
    case_id: str
    state: RunState
    recommendation: Recommendation
    reason: str = ""
    request: ProcessingRequest | None = None
    sourced_facts: list[SourcedFact] = Field(default_factory=list)
    calculations: list[Calculation] = Field(default_factory=list)
    inferences: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    policy_findings: list[PolicyFinding] = Field(default_factory=list)
    exceptions: list[CaseException] = Field(default_factory=list)
    confidence: Confidence
    next_action: NextAction
    approval: ApprovalView | None = None
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    events: list[AuditEvent] = Field(default_factory=list)
    llm_prompt: dict[str, Any] | None = None
    llm_response: str | None = None


class ApprovalDecision(_Model):
    action: ApprovalAction
    approver_id: str
    role: str
    comment: str = ""
