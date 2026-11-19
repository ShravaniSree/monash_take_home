"""Deterministic accounts-payable checks.

Outcome codes come from this module. A model narrative cannot change them.
Authority limits are the FIN-POL-003 v4.0 table, not a retrieved chunk.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from ap_agent.domain.models import (
    Calculation,
    CaseException,
    Confidence,
    ExceptionCategory,
    InvoiceHistoryMatch,
    MatchType,
    NextAction,
    NonPoJustification,
    PolicyFinding,
    PurchaseOrder,
    Recommendation,
    RunState,
    SourcedFact,
    ToolError,
    VendorRecord,
    VendorStatus,
)

_Q2 = Decimal("0.01")
_LIMITS: list[tuple[str, Decimal | None]] = [
    ("COST_CENTRE_MANAGER", Decimal("10000")),
    ("DEPARTMENT_DIRECTOR", Decimal("50000")),
    ("EXECUTIVE_DIRECTOR", Decimal("250000")),
    ("CFO", Decimal("1000000")),
    ("CEO", None),
]
_BLOCKED = {
    VendorStatus.BLOCKED,
    VendorStatus.DORMANT,
    VendorStatus.SANCTIONS_REVIEW,
    VendorStatus.PENDING_VERIFICATION,
}
_PRECEDENCE = [
    ExceptionCategory.DUPLICATE_RISK,
    ExceptionCategory.VENDOR_BLOCK,
    ExceptionCategory.BANK_CHANGE,
    ExceptionCategory.MISSING_RECEIPT,
    ExceptionCategory.MISSING_PO,
    ExceptionCategory.QUANTITY_VARIANCE,
    ExceptionCategory.PRICE_VARIANCE,
    ExceptionCategory.AUTHORITY_GAP,
    ExceptionCategory.TAX_QUERY,
    ExceptionCategory.OTHER_CONTROL_RISK,
]


def money(value: Decimal) -> str:
    """Round a money amount to cents with half-up rounding.

    Args:
        value: Unrounded decimal amount.

    Returns:
        The rounded amount as a string, so JSON keeps the exact digits.
    """
    return str(value.quantize(_Q2, rounding=ROUND_HALF_UP))


def normalise_invoice_ref(value: str) -> str:
    """Strip punctuation and case so invoice numbers can be compared.

    Args:
        value: Invoice number as printed on the document.

    Returns:
        Uppercase letters and digits only.
    """
    return "".join(character for character in value.upper() if character.isalnum())


def role_for_amount(amount_aud: Decimal) -> str:
    """Choose the lowest FIN-POL-003 v4.0 role that can approve the amount.

    Args:
        amount_aud: Invoice amount converted to AUD.

    Returns:
        Role name from the delegated-authority table.
    """
    for role, limit in _LIMITS:
        if limit is None or amount_aud <= limit:
            return role
    return "CEO"


def _add_business_days(start: date, days: int) -> date:
    """Move forward by a number of weekdays.

    Args:
        start: First day, which is not itself counted.
        days: Weekdays to add.

    Returns:
        The date on which a hold of ``days`` business days has elapsed.
    """
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def bank_hold_active(verified_on: date, on: date) -> bool:
    """Return whether a verified bank change is still inside its payment hold.

    Args:
        verified_on: Date the vendor master recorded the verification.
        on: Invoice date being considered for payment.

    Returns:
        True when ``on`` is earlier than two business days after verification.
    """
    return on < _add_business_days(verified_on, 2)


def _line_cap(kind: str, line_total: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Return the absolute cap, percent, and the lower of the two.

    Args:
        kind: ``goods`` or ``services``.
        line_total: Purchase-order line value.

    Returns:
        Absolute cap, percent cap, and the threshold that applies.
    """
    if kind == "goods":
        cap_abs, cap_pct = Decimal("50"), Decimal("0.01")
    else:
        cap_abs, cap_pct = Decimal("100"), Decimal("0.02")
    percent = (line_total * cap_pct).quantize(_Q2, rounding=ROUND_HALF_UP)
    return cap_abs, cap_pct, min(cap_abs, percent)


def role_can_approve(role: str, amount_aud: Decimal) -> bool:
    """Check whether a role's limit covers the AUD amount.

    Financial Control is an extra reviewer on high-risk cases. That role
    does not itself authorise a payment.

    Args:
        role: Role named on the approval payload.
        amount_aud: Invoice amount converted to AUD.

    Returns:
        True when the role may approve this amount.
    """
    if role == "FINANCIAL_CONTROL":
        return False
    limits = dict(_LIMITS)
    limit = limits.get(role)
    if limit is None and role == "CEO":
        return True
    if limit is None:
        return False
    return amount_aud <= limit


class EngineResult:
    """Mutable outcome of one reconciliation, before it becomes a ``RunResult``."""

    def __init__(self) -> None:
        """Start from a closed hold. Passing checks replace this outcome."""
        self.recommendation = Recommendation.HOLD_FOR_INFORMATION
        self.next_action = NextAction.STOP_NO_PAYMENT
        self.state = RunState.COMPLETED
        self.facts: list[SourcedFact] = []
        self.calculations: list[Calculation] = []
        self.inferences: list[str] = []
        self.unknowns: list[str] = []
        self.findings: list[PolicyFinding] = []
        self.exceptions: list[CaseException] = []
        self.indicators: list[str] = []
        self.required_roles: list[str] = []
        self.amount_aud = Decimal("0")
        self.confidence = Confidence(level="low", reasons=["incomplete"])
        self.primary: ExceptionCategory | None = None
        self.reason = "held for information"

    def add_exception(
        self,
        category: ExceptionCategory,
        failed_rule: str,
        expected: str,
        observed: str,
        owner: str,
        chunk_id: str | None,
    ) -> None:
        """Record one failed control.

        Args:
            category: Exception family used for precedence.
            failed_rule: Stable rule id.
            expected: What the control required.
            observed: What the evidence showed.
            owner: Team that must review the exception.
            chunk_id: Current policy chunk, when one was retrieved.
        """
        self.exceptions.append(
            CaseException(
                category=category,
                failed_rule=failed_rule,
                expected=expected,
                observed=observed,
                chunk_id=chunk_id,
                owner=owner,
                next_review_date=date.today() + timedelta(days=3),
            )
        )


def _hits(citations: dict | list) -> list[dict]:
    """Return the chunks from the retrieval response.

    Args:
        citations: ``retrieve_finance_documents`` chunks, or a legacy map
            of chunk ids used by unit tests.

    Returns:
        Hit dictionaries. A legacy map has no text to match.
    """
    if isinstance(citations, dict):
        return [{"chunk_id": chunk_id, "text": ""} for chunk_id in citations.values()]
    return [hit for hit in citations if isinstance(hit, dict) and hit.get("chunk_id")]


def _chunk_for(citations: dict | list, *terms: str) -> str | None:
    """Pick a retrieved chunk that mentions this check.

    Args:
        citations: Chunks returned for this case.
        terms: Words for the check, such as ``duplicate`` or ``receipt``.

    Returns:
        The best trusted current chunk id, or ``None`` when retrieval
        did not return a matching chunk.
    """
    needles = [term.lower() for term in terms if term]
    best: tuple[tuple[int, float], str] | None = None
    for hit in _hits(citations):
        if hit.get("stale") or str(hit.get("trust") or "trusted") == "untrusted":
            continue
        if hit.get("status") not in (None, "current"):
            continue
        blob = " ".join(str(hit.get(key) or "") for key in ("title", "section", "text")).lower()
        overlap = sum(needle in blob for needle in needles)
        if overlap == 0:
            continue
        rank = (overlap, float(hit.get("score") or 0))
        if best is None or rank > best[0]:
            best = (rank, str(hit["chunk_id"]))
    return None if best is None else best[1]


def _finding(result: EngineResult, rule_id: str, outcome: str, chunk_id: str | None) -> None:
    """Attach a pass or fail finding, or record that the citation is missing.

    Args:
        result: Outcome being built.
        rule_id: Stable rule id.
        outcome: ``pass`` or ``fail``.
        chunk_id: Current policy chunk. A missing chunk is an unknown,
            not a silent pass.
    """
    if chunk_id is None:
        result.unknowns.append(f"{rule_id} has no current citation")
        return
    result.findings.append(PolicyFinding(rule_id=rule_id, result=outcome, chunk_id=chunk_id))  # type: ignore[arg-type]


def reconcile(
    *,
    invoice_ref: str,
    vendor_name: str,
    amount: Decimal,
    currency: str,
    invoice_date: date | None,
    requester_id: str | None,
    receipter_id: str | None,
    vendor: VendorRecord | None,
    vendor_error: ToolError | None,
    purchase_order: PurchaseOrder | None,
    po_error: ToolError | None,
    history: list[InvoiceHistoryMatch],
    indicators: list[str],
    citations: dict | list,
    aud_rate: Decimal | None,
    high_risk_flags: list[str] | None = None,
    invoice_lines: list | None = None,
    freight: Decimal | None = None,
    service_confirmed: bool = False,
    non_po_justification: NonPoJustification | None = None,
) -> EngineResult:
    """Apply the accounts-payable controls in code.

    The order is fixed: exact duplicate, vendor, purchase order and
    receipt, tolerance, fraud indicators, segregation of duties, then
    authority. A model narrative cannot change the returned recommendation.

    Args:
        invoice_ref: Invoice number.
        vendor_name: Name printed on the invoice.
        amount: Gross invoice amount in ``currency``.
        currency: ISO currency code.
        invoice_date: Invoice date, when present.
        requester_id: Person who raised the purchase.
        receipter_id: Person who confirmed receipt.
        vendor: Vendor master row, or ``None`` when the lookup failed.
        vendor_error: Tool error from the vendor lookup.
        purchase_order: Purchase order, or ``None`` when it is missing.
        po_error: Tool error from the purchase-order lookup.
        history: Prior invoices returned for this vendor.
        indicators: Untrusted-text signals already detected.
        citations: Chunks returned by ``retrieve_finance_documents`` for this case.
        aud_rate: AUD per unit of ``currency``.
        high_risk_flags: Extra risk flags supplied by the caller.
        invoice_lines: Invoiced quantity and amount per line, when the
            request states them.
        freight: Freight amount, kept out of the line-price variance.
        service_confirmed: True when the service owner confirmed completion.
        non_po_justification: Structured requester claim for a non-PO case.
            It is not an approval and cannot satisfy procurement review.

    Returns:
        Recommendation, exceptions, calculations, and the approval role
        when a person still has to decide.
    """
    result = EngineResult()
    result.indicators = list(indicators)
    flags = list(high_risk_flags or [])

    if amount <= 0:
        result.recommendation = Recommendation.REJECT_INVALID
        result.next_action = NextAction.STOP_NO_PAYMENT
        result.add_exception(
            ExceptionCategory.OTHER_CONTROL_RISK,
            "invoice.amount",
            "gross amount greater than zero",
            money(amount),
            "Accounts Payable Operations",
            _chunk_for(citations, "gross amount"),
        )
        _finding(result, "invoice.amount", "fail", _chunk_for(citations, "gross amount"))
        _finish(result, missing_tools=False)
        return result

    exact = [
        item
        for item in history
        if item.match_type is MatchType.EXACT and item.status.value in {"PAID", "POSTED"}
    ]
    if exact:
        result.recommendation = Recommendation.REJECT_DUPLICATE
        result.next_action = NextAction.STOP_NO_PAYMENT
        result.add_exception(
            ExceptionCategory.DUPLICATE_RISK,
            "duplicate.exact",
            "duplicate record found in invoice",
            f"matched {exact[0].record_id} status {exact[0].status.value}",
            "Accounts Payable Operations",
            _chunk_for(citations, "duplicate"),
        )
        _finding(result, "duplicate.exact", "fail", _chunk_for(citations, "duplicate"))
        # Duplicate detection is terminal, but retain the separate missing-PO
        # exception so the record exposes both facts without weakening the
        # duplicate rejection.
        no_po_submitted = (
            po_error is not None
            and po_error.code.value == "NOT_FOUND"
            and "no purchase order" in po_error.message.lower()
        )
        if purchase_order is None and no_po_submitted:
            non_po_policy = _chunk_for(citations, "emergency", "purchase")
            observed_non_po = (
                f"category {non_po_justification.category.value} supplied; "
                "supporting evidence and procurement review are not verified"
                if non_po_justification
                else "no purchase order or non-PO justification supplied"
            )
            result.add_exception(
                ExceptionCategory.MISSING_PO,
                "non_po.missing",
                "eligible non-PO category, supporting evidence, and verified procurement review",
                observed_non_po,
                "Procurement Governance",
                non_po_policy,
            )
            _finding(result, "non_po.missing", "fail", non_po_policy)
        result.facts.append(
            SourcedFact(
                name="duplicate_record",
                value=exact[0].record_id,
                source={"kind": "tool", "tool": "check_invoice_history", "record_id": exact[0].record_id},
            )
        )
        _finish(result, missing_tools=False)
        return result

    fuzzy = [item for item in history if item.match_type is MatchType.FUZZY and item.status.value != "REJECTED"]
    if fuzzy:
        result.add_exception(
            ExceptionCategory.DUPLICATE_RISK,
            "duplicate.fuzzy",
            "no probable duplicate",
            f"probable match {fuzzy[0].record_id}",
            "Accounts Payable Operations",
            _chunk_for(citations, "duplicate"),
        )
        _finding(result, "duplicate.fuzzy", "fail", _chunk_for(citations, "duplicate"))
    rejected_only = [item for item in history if item.status.value == "REJECTED"]
    if rejected_only and not exact and not fuzzy:
        result.unknowns.append(
            f"prior rejection {rejected_only[0].record_id} is not an automatic duplicate"
        )

    if vendor_error is not None or vendor is None:
        result.add_exception(
            ExceptionCategory.OTHER_CONTROL_RISK,
            "vendor.missing",
            "vendor master record",
            vendor_error.message if vendor_error else "missing vendor",
            "Vendor Governance",
            _chunk_for(citations, "vendor"),
        )
        _hold(result, missing_tools=True)
        return result

    result.facts.append(
        SourcedFact(
            name="vendor_status",
            value=vendor.status.value,
            source={"kind": "tool", "tool": "get_vendor_record", "record_id": vendor.vendor_id},
        )
    )
    if vendor.status in _BLOCKED:
        result.add_exception(
            ExceptionCategory.VENDOR_BLOCK,
            "vendor.status",
            "ACTIVE",
            vendor.status.value,
            "Vendor Governance",
            _chunk_for(citations, "vendor"),
        )
        _finding(result, "vendor.status", "fail", _chunk_for(citations, "vendor"))
        _hold(result, missing_tools=False)
        return result
    _finding(result, "vendor.status", "pass", _chunk_for(citations, "vendor"))

    if vendor.legal_name.strip().lower() != vendor_name.strip().lower():
        result.indicators.append("NAME_MISMATCH")
    if vendor.payment_country != vendor.country:
        flags.append("OVERSEAS_ACCOUNT")
    if vendor.bank_change_verified_at is not None:
        flags.append("BANK_CHANGE")
    if invoice_date is not None and vendor.created_at.date() > invoice_date - timedelta(days=30):
        flags.append("NEW_VENDOR")

    if po_error is not None or purchase_order is None:
        code = po_error.code.value if po_error else "NOT_FOUND"
        category = ExceptionCategory.MISSING_PO
        non_po_policy = _chunk_for(citations, "emergency", "purchase")
        if non_po_justification is not None:
            observed = (
                f"category {non_po_justification.category.value}; reason and evidence references supplied; "
                "retrospective procurement review is not verified"
            )
        else:
            observed = f"purchase order unavailable ({code}); no non-PO justification supplied"
        result.add_exception(
            category,
            "non_po.missing" if po_error and "no purchase order" in po_error.message.lower() else "purchase_order.missing",
            "eligible non-PO category, reason, receipt evidence, cost centre, and verified retrospective review",
            observed,
            "Procurement Governance" if po_error and "no purchase order" in po_error.message.lower() else "Procure-to-Pay Operations",
            non_po_policy if po_error and "no purchase order" in po_error.message.lower() else _chunk_for(citations, "purchase order", "receipt"),
        )
        rule_id = "non_po.missing" if po_error and "no purchase order" in po_error.message.lower() else "purchase_order.missing"
        cited_policy = non_po_policy if rule_id == "non_po.missing" else _chunk_for(citations, "purchase order", "receipt")
        _finding(result, rule_id, "fail", cited_policy)
        if non_po_justification is not None:
            result.unknowns.append("non-PO justification was supplied but required evidence and procurement review are not verified")
        _hold(result, missing_tools=po_error is not None and po_error.code.value in {"TIMEOUT", "TRANSIENT"})
        return result

    if purchase_order.currency != currency and not purchase_order.fx_permitted:
        result.add_exception(
            ExceptionCategory.OTHER_CONTROL_RISK,
            "currency.mismatch",
            purchase_order.currency,
            currency,
            "Treasury",
            _chunk_for(citations, "currency"),
        )
    elif purchase_order.currency == currency:
        _finding(result, "currency.match", "pass", _chunk_for(citations, "currency"))

    rate = aud_rate if currency != "AUD" else Decimal("1")
    if rate is None:
        result.unknowns.append("no corporate AUD rate for the invoice date")
        result.amount_aud = amount
    else:
        result.amount_aud = (amount * rate).quantize(_Q2, rounding=ROUND_HALF_UP)
        result.calculations.append(
            Calculation(
                name="aud_conversion",
                inputs={"gross": money(amount), "currency": currency, "rate": str(rate)},
                formula="aud = gross * rate",
                result={"amount_aud": money(result.amount_aud)},
                rounding="ROUND_HALF_UP/2",
            )
        )

    received = {item.line: item.qty for item in purchase_order.receipts}
    billed = {item.line: item for item in (invoice_lines or [])}
    if not billed:
        result.inferences.append(
            "invoice line quantity was not supplied; purchase-order quantity was compared with the receipt"
        )
        result.unknowns.append("invoice line quantity was not on the request")
    freight_amount = freight or Decimal("0")
    if freight is not None:
        if not purchase_order.freight_permitted or freight_amount > Decimal("75"):
            result.add_exception(
                ExceptionCategory.PRICE_VARIANCE,
                "freight",
                "freight at most 75 and permitted on the purchase order",
                f"freight {money(freight_amount)} permitted {purchase_order.freight_permitted}",
                "Procure-to-Pay Operations",
                _chunk_for(citations, "freight"),
            )
        result.calculations.append(
            Calculation(
                name="freight",
                inputs={
                    "freight": money(freight_amount),
                    "permitted": purchase_order.freight_permitted,
                    "cap": "75.00",
                },
                formula="freight <= 75 when the purchase order permits freight",
                result={"within": purchase_order.freight_permitted and freight_amount <= Decimal("75")},
                rounding="ROUND_HALF_UP/2",
            )
        )
    goods_amount = amount - freight_amount
    single_line = len(purchase_order.lines) == 1
    for line in purchase_order.lines:
        got = received.get(line.line)
        if billed:
            invoice_line = billed.get(line.line)
            if invoice_line is None:
                result.add_exception(
                    ExceptionCategory.QUANTITY_VARIANCE,
                    "quantity",
                    f"invoice line {line.line}",
                    "line missing on the invoice",
                    "Receipter",
                    _chunk_for(citations, "quantity"),
                )
                continue
            invoiced_qty = invoice_line.qty
            invoiced_amount = invoice_line.line_total
        else:
            invoiced_qty = line.qty
            invoiced_amount = goods_amount if single_line else line.line_total
        if got is None:
            result.add_exception(
                ExceptionCategory.MISSING_RECEIPT,
                "receipt",
                f"receipt for line {line.line}",
                "no receipt",
                "Receipter",
                _chunk_for(citations, "receipt"),
            )
            continue
        if invoiced_qty > got:
            result.add_exception(
                ExceptionCategory.QUANTITY_VARIANCE,
                "quantity",
                f"invoiced qty <= received {got}",
                f"invoiced qty {invoiced_qty}",
                "Receipter",
                _chunk_for(citations, "quantity"),
            )
        cap_abs, cap_pct, threshold = _line_cap(line.kind, line.line_total)
        difference = abs(invoiced_amount - line.line_total)
        within = difference <= threshold
        if line.kind == "services" and not service_confirmed:
            within = False
            result.add_exception(
                ExceptionCategory.PRICE_VARIANCE,
                "service.confirmation",
                "service owner confirmation",
                "confirmation missing",
                "Service owner",
                _chunk_for(citations, "service"),
            )
        result.calculations.append(
            Calculation(
                name="line_tolerance",
                inputs={
                    "line": line.line,
                    "invoice_line": money(invoiced_amount),
                    "po_line_value": money(line.line_total),
                    "abs_cap": money(cap_abs),
                    "pct_cap": str(cap_pct),
                    "invoiced_qty": str(invoiced_qty),
                    "received_qty": str(got),
                },
                formula="abs(invoice_line-po_line) <= min(abs_cap, pct_cap * po_line) and invoiced_qty <= received_qty",
                result={
                    "difference": money(difference),
                    "threshold": money(threshold),
                    "within": within and invoiced_qty <= got,
                },
                rounding="ROUND_HALF_UP/2",
            )
        )
        if line.kind != "services" and not within:
            result.add_exception(
                ExceptionCategory.PRICE_VARIANCE,
                "tolerance",
                f"difference <= {money(threshold)}",
                f"difference {money(difference)}",
                "Procure-to-Pay Operations",
                _chunk_for(citations, "tolerance"),
            )
    if any(item.category is ExceptionCategory.QUANTITY_VARIANCE for item in result.exceptions):
        _finding(result, "quantity", "fail", _chunk_for(citations, "quantity"))
    price_failed = any(
        item.failed_rule in {"tolerance", "service.confirmation", "freight"}
        for item in result.exceptions
    )
    if price_failed:
        _finding(result, "tolerance", "fail", _chunk_for(citations, "tolerance"))
    elif not any(item.category is ExceptionCategory.MISSING_RECEIPT for item in result.exceptions):
        _finding(result, "tolerance", "pass", _chunk_for(citations, "tolerance"))

    if any(item.category is ExceptionCategory.MISSING_RECEIPT for item in result.exceptions):
        _finding(result, "receipt", "fail", _chunk_for(citations, "receipt"))
        _hold(result, missing_tools=False)
        return result

    distinct_indicators = list(dict.fromkeys(result.indicators))
    result.indicators = distinct_indicators
    if len(distinct_indicators) >= 2:
        result.recommendation = Recommendation.ESCALATE_CONTROL_REVIEW
        result.next_action = NextAction.ESCALATE
        result.add_exception(
            ExceptionCategory.OTHER_CONTROL_RISK,
            "fraud.indicators",
            "fewer than two fraud indicators",
            ", ".join(distinct_indicators),
            "Financial Crime and Controls",
            _chunk_for(citations, "duplicate"),
        )
        _finding(result, "fraud.indicators", "fail", _chunk_for(citations, "duplicate"))
        result.facts.append(
            SourcedFact(
                name="fraud_indicators",
                value=distinct_indicators,
                source={
                    "kind": "chunk",
                    "chunk_id": _chunk_for(citations, "duplicate"),
                    "document_id": None
                    if _chunk_for(citations, "duplicate") is None
                    else _chunk_for(citations, "duplicate").split("#", 1)[0],
                },
            )
        )
        _finish(result, missing_tools=False)
        return result

    if result.amount_aud > Decimal("25000") and requester_id and receipter_id and requester_id == receipter_id:
        result.recommendation = Recommendation.ESCALATE_CONTROL_REVIEW
        result.next_action = NextAction.ESCALATE
        result.add_exception(
            ExceptionCategory.AUTHORITY_GAP,
            "segregation",
            "requester and receipter are different people",
            requester_id,
            "Financial Control",
            _chunk_for(citations, "receipter"),
        )
        _finding(result, "segregation", "fail", _chunk_for(citations, "receipter"))
        _finish(result, missing_tools=False)
        return result

    if (
        vendor.bank_change_verified_at is not None
        and invoice_date is not None
        and bank_hold_active(vendor.bank_change_verified_at.date(), invoice_date)
    ):
        result.add_exception(
            ExceptionCategory.BANK_CHANGE,
            "bank_change.hold",
            "two business days after the verified bank change",
            vendor.bank_change_verified_at.date().isoformat(),
            "Vendor Governance",
            None,
        )
        result.unknowns.append("bank-change policy text is restricted and was not retrieved")
        _hold(result, missing_tools=False)
        return result

    if flags:
        result.required_roles = [role_for_amount(result.amount_aud), "FINANCIAL_CONTROL"]
    else:
        result.required_roles = [role_for_amount(result.amount_aud)]

    blocking = [
        item
        for item in result.exceptions
        if item.category is not ExceptionCategory.BANK_CHANGE
    ]
    rate_unknown = any("corporate AUD rate" in item for item in result.unknowns)
    if blocking or rate_unknown:
        _hold(result, missing_tools=False)
        _finding(result, "outcome", "fail", _chunk_for(citations, "approval"))
        return result

    if "BANK_CHANGE" in flags or "OVERSEAS_ACCOUNT" in flags or "NEW_VENDOR" in flags:
        _finding(result, "authority.high_risk", "pass", _chunk_for(citations, "authority"))
    _finding(result, "authority.limit", "pass", _chunk_for(citations, "authority"))
    _finding(result, "outcome", "pass", _chunk_for(citations, "approval"))
    result.recommendation = Recommendation.APPROVE_FOR_POSTING
    result.next_action = NextAction.REQUEST_APPROVAL
    result.state = RunState.AWAITING_APPROVAL
    _finish(result, missing_tools=False)
    return result


def _hold(result: EngineResult, *, missing_tools: bool) -> None:
    """Close the case as a hold and set confidence.

    Args:
        result: Outcome being built.
        missing_tools: True when a required system did not answer, so the
            next action is to retry rather than stop.
    """
    result.recommendation = Recommendation.HOLD_FOR_INFORMATION
    result.next_action = NextAction.RETRY_EVIDENCE if missing_tools else NextAction.STOP_NO_PAYMENT
    result.state = RunState.COMPLETED
    _finish(result, missing_tools=missing_tools)


def _finish(result: EngineResult, *, missing_tools: bool) -> None:
    """Set the primary exception, the recommendation reason, and the confidence band.

    Args:
        result: Outcome being built.
        missing_tools: True when a required system did not answer.
    """
    if result.exceptions:
        result.primary = sorted(
            {item.category for item in result.exceptions},
            key=_PRECEDENCE.index,
        )[0]
        chosen = next(item for item in result.exceptions if item.category is result.primary)
        result.reason = f"{chosen.failed_rule}: expected {chosen.expected}; observed {chosen.observed}"
    elif result.recommendation is Recommendation.APPROVE_FOR_POSTING:
        result.reason = "required checks passed"
    elif result.unknowns:
        result.reason = "; ".join(result.unknowns)
    else:
        result.reason = result.recommendation.value
    if result.recommendation is Recommendation.APPROVE_FOR_POSTING and not missing_tools:
        result.confidence = Confidence(level="high", reasons=["required tools returned", "limits taken from the delegated authority table"])
    elif missing_tools:
        result.confidence = Confidence(level="low", reasons=["a required tool was unavailable"])
    else:
        result.confidence = Confidence(level="medium", reasons=["a control failed closed"])
