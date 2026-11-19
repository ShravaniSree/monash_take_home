"""Collect case evidence and turn a rules-engine result into a run snapshot.

The workflow calls this module. It does not choose the recommendation.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ap_agent.domain.engine import EngineResult
from ap_agent.domain.engine import reconcile
from ap_agent.domain.guard import scan_many
from ap_agent.domain.models import ApprovalView
from ap_agent.domain.models import AuditEvent
from ap_agent.domain.models import InvoiceHistoryMatch
from ap_agent.domain.models import ProcessingRequest
from ap_agent.domain.models import PurchaseOrder
from ap_agent.domain.models import RunResult
from ap_agent.domain.models import RunState
from ap_agent.domain.models import ToolCallRecord
from ap_agent.domain.models import ToolError
from ap_agent.domain.models import ToolErrorCode
from ap_agent.domain.models import Trust
from ap_agent.domain.models import VendorRecord
from ap_agent.domain.redact import redact_data
from ap_agent.retrieval.index import PolicyIndex
from ap_agent.systems.world import FinanceWorld
from ap_agent.systems.world import ToolFault

_MAX_ATTEMPTS = 3
_TOOL_BUDGET = 15
_RETRYABLE = {ToolErrorCode.TIMEOUT, ToolErrorCode.TRANSIENT}
_DEFAULT_TIMEOUT = 3.0
_RETRIEVAL_TIMEOUT = 8.0

ToolValue = Any
ToolOutcome = tuple[ToolValue, int, ToolError | None]


def _jsonable(value: Any) -> Any:
    """Convert a tool result into JSON-ready data for the trace.

    Args:
        value: A model, a list of models, or a plain value.

    Returns:
        Data that ``json.dumps`` can encode.
    """
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def _record(calls: list[dict], record: ToolCallRecord) -> None:
    """Append one attempt to the trace stored on the run.

    Args:
        calls: Trace collected during evidence gathering.
        record: One request paired with its response or error.
    """
    calls.append(record.model_dump(mode="json"))


def _run_with_timeout(func: Callable[[], ToolValue], timeout_s: float) -> ToolValue:
    """Run a tool body and stop waiting when the deadline passes.

    Args:
        func: Zero-argument tool body.
        timeout_s: Maximum seconds to wait.

    Returns:
        The tool body result.

    Raises:
        ToolFault: The body did not finish within ``timeout_s``.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(func)
    try:
        return future.result(timeout=timeout_s)
    except FuturesTimeoutError as exc:
        raise ToolFault(ToolErrorCode.TIMEOUT, "tool exceeded its time limit") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def call_tool(
    calls: list[dict],
    tool: str,
    request: dict,
    func: Callable[[], ToolValue],
    timeout_s: float = _DEFAULT_TIMEOUT,
) -> ToolOutcome:
    """Call one simulated system, retrying timeout and transient faults.

    Each attempt is recorded with a timestamp and a duration, including
    the failed ones. A call past the tool budget is not executed.

    Args:
        calls: Trace to append to.
        tool: Tool name shown in the trace.
        request: Arguments sent to the tool.
        func: Zero-argument call into the simulated system.
        timeout_s: Wall-clock limit for one attempt.

    Returns:
        The successful value, the attempt count, and ``None``, or ``None``,
        the attempt count, and the last error.
    """
    stored_request = redact_data(request)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if len(calls) >= _TOOL_BUDGET:
            error = ToolError(code=ToolErrorCode.BUDGET_EXCEEDED, message="tool budget exhausted")
            _record(
                calls,
                ToolCallRecord(tool=tool, attempt=attempt, request=stored_request, error=error, ts=_now(), duration_ms=0),
            )
            return None, attempt, error
        started = time.perf_counter()
        try:
            value = _run_with_timeout(func, timeout_s)
        except ToolFault as exc:
            error = ToolError(code=exc.code, message=exc.message)
            _record(
                calls,
                ToolCallRecord(
                    tool=tool,
                    attempt=attempt,
                    request=stored_request,
                    error=error,
                    ts=_now(),
                    duration_ms=_elapsed(started),
                ),
            )
            if exc.code not in _RETRYABLE or attempt == _MAX_ATTEMPTS:
                return None, attempt, error
            continue
        response = _jsonable(value)
        if tool != "retrieve_finance_documents":
            response = redact_data(response)
        _record(
            calls,
            ToolCallRecord(
                tool=tool,
                attempt=attempt,
                request=stored_request,
                response=response,
                ts=_now(),
                duration_ms=_elapsed(started),
            ),
        )
        return value, attempt, None
    error = ToolError(code=ToolErrorCode.TRANSIENT, message="tool failed")
    return None, _MAX_ATTEMPTS, error


def _now() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _elapsed(started: float) -> int:
    """Return whole milliseconds since ``started``."""
    return int((time.perf_counter() - started) * 1000)


def case_intents(
    request: ProcessingRequest,
    *,
    vendor: VendorRecord | None = None,
    purchase_order: PurchaseOrder | None = None,
    indicators: list[str] | None = None,
) -> list[dict[str, object]]:
    """Build narrow policy searches from typed fields and tool evidence.

    Supplier text is never copied into a query. Scanned signal names can
    trigger a relevant control topic, but the supplied text remains untrusted
    evidence and cannot satisfy that control.
    """
    intents: list[dict[str, object]] = [
        {
            "name": "core_processing",
            "query": "accounts payable invoice evidence processing recommendation approval control",
            "topics": ["accounts-payable", "invoice", "controls"],
        },
        {
            "name": "duplicate_detection",
            "query": "duplicate invoice exact match paid posted fuzzy prior invoice",
            "topics": ["duplicate", "invoice"],
        },
        {
            "name": "vendor_status",
            "query": "vendor status active blocked onboarding verification invoice payment",
            "topics": ["vendor", "onboarding"],
        },
        {
            "name": "delegated_authority",
            "query": "delegated financial authority approval limits gross commitment AUD roles",
            "topics": ["approval", "authority"],
        },
    ]
    if request.po_number:
        intents.append(
            {
                "name": "po_receipt_matching",
                "query": "purchase order goods services receipt line quantity price variance tolerance",
                "topics": ["purchase-order", "receipt", "tolerance", "matching"],
            }
        )
    else:
        non_po_terms = {
            "STATUTORY_CHARGE": "statutory charges",
            "APPROVED_UTILITY": "approved utilities",
            "PROPERTY_LEASE": "property lease",
            "PROFESSIONAL_MEMBERSHIP": "approved professional membership",
            "GENUINE_EMERGENCY": "genuine emergency safety outage asset loss",
        }
        category_terms = (
            non_po_terms.get(request.non_po_justification.category.value, "")
            if request.non_po_justification
            else ""
        )
        intents.append(
            {
                "name": "non_po_exception",
                "query": f"non purchase order emergency purchase approved justification {category_terms}".strip(),
                "topics": ["emergency", "purchase"],
            }
        )
    po_currency = purchase_order.currency if purchase_order else None
    if request.currency != "AUD" or (po_currency and po_currency != request.currency):
        intents.append(
            {
                "name": "foreign_currency",
                "query": "foreign currency invoice purchase order currency agreement exchange rate AUD conversion",
                "topics": ["foreign-exchange", "currency", "invoice"],
            }
        )
    if request.freight is not None:
        intents.append(
            {
                "name": "freight",
                "query": "freight charge purchase order permitted freight threshold",
                "topics": ["freight", "purchase-order"],
            }
        )
    signal_set = set(indicators or [])
    vendor_bank_change = bool(
        vendor
        and (
            vendor.bank_change_verified_at
            or any("bank" in flag.lower() for flag in vendor.risk_flags)
        )
    )
    if "BANK_CHANGE_LANGUAGE" in signal_set or vendor_bank_change:
        intents.append(
            {
                "name": "bank_change_control",
                "query": "bank account change independent verification payment hold Financial Control",
                "topics": ["vendor", "bank-account", "verification"],
            }
        )
    if signal_set.intersection({"BYPASS_REQUEST", "URGENT_OR_SECRET"}):
        intents.append(
            {
                "name": "fraud_and_instruction_controls",
                "query": "fraud indicators urgent secret payment bypass approval duplicate detection",
                "topics": ["fraud", "controls"],
            }
        )
    return intents


def _retrieve_policy(
    index: PolicyIndex,
    request: ProcessingRequest,
    calls: list[dict],
    *,
    vendor: VendorRecord | None = None,
    purchase_order: PurchaseOrder | None = None,
    indicators: list[str] | None = None,
) -> list:
    """Retrieve a bounded set of eligible policy chunks for case intents."""
    intents = case_intents(request, vendor=vendor, purchase_order=purchase_order, indicators=indicators)
    chunks, _attempts, _error = call_tool(
        calls,
        "retrieve_finance_documents",
        {"intents": intents, "role": "ap_processor", "top_k": 12},
        lambda: index.retrieve_for_case("", intents=intents, role="ap_processor", top_k=12),
        timeout_s=_RETRIEVAL_TIMEOUT,
    )
    return chunks or []


def citations_from_hits(chunks: list) -> dict[str, str]:
    """Choose the best current chunk retrieved for each trusted document.

    Any document the search returned can be cited. Restricted, superseded,
    and untrusted chunks are not citations. A document that was not in
    ``chunks`` cannot be cited, including one added to the corpus later
    but not ingested.

    Args:
        chunks: Chunks returned for this case.

    Returns:
        Document id to chunk id, for every trusted current hit.
    """
    best: dict[str, object] = {}
    for chunk in chunks:
        if chunk.status != "current" or chunk.stale or chunk.trust is not Trust.TRUSTED:
            continue
        if chunk.classification not in {"internal", "external-unverified"}:
            continue
        current = best.get(chunk.document_id)
        if current is None or chunk.score > current.score:
            best[chunk.document_id] = chunk
    return {document_id: chunk.chunk_id for document_id, chunk in best.items()}


def _lookup_vendor(world: FinanceWorld, request: ProcessingRequest, calls: list[dict]) -> ToolOutcome:
    """Read the vendor master record.

    Args:
        world: Simulated systems for this case.
        request: Invoice case.
        calls: Trace to append to.

    Returns:
        Vendor record, attempt count, and any tool error.
    """
    return call_tool(
        calls,
        "get_vendor_record",
        {"vendor_id": request.vendor_id},
        lambda: world.get_vendor(request.vendor_id),
    )


def _lookup_purchase_order(world: FinanceWorld, request: ProcessingRequest, calls: list[dict]) -> ToolOutcome:
    """Read the purchase order, or record that the invoice has none.

    Args:
        world: Simulated systems for this case.
        request: Invoice case.
        calls: Trace to append to.

    Returns:
        Purchase order, attempt count, and any tool error.
    """
    if not request.po_number:
        error = ToolError(code=ToolErrorCode.NOT_FOUND, message="no purchase order on the request")
        _record(
            calls,
            ToolCallRecord(
                tool="get_purchase_order",
                attempt=1,
                request={"po_number": None},
                error=error,
                ts=_now(),
                duration_ms=0,
            ),
        )
        return None, 1, error
    return call_tool(
        calls,
        "get_purchase_order",
        {"po_number": request.po_number},
        lambda: world.get_purchase_order(request.po_number or ""),
    )


def _history_request(request: ProcessingRequest) -> dict[str, Any]:
    """Build the invoice-history query from the case.

    Args:
        request: Invoice case.

    Returns:
        JSON-ready arguments for ``check_invoice_history``.
    """
    return {
        "vendor_id": request.vendor_id,
        "invoice_ref": request.invoice_ref,
        "invoice_date": None if request.invoice_date is None else request.invoice_date.isoformat(),
        "currency": request.currency,
        "gross_amount": format(request.amount, "f"),
        "po_number": request.po_number,
    }


def _lookup_history(world: FinanceWorld, request: ProcessingRequest, calls: list[dict]) -> ToolOutcome:
    """Search prior invoices for an exact or fuzzy duplicate.

    Args:
        world: Simulated systems for this case.
        request: Invoice case.
        calls: Trace to append to.

    Returns:
        Matching history rows, attempt count, and any tool error.
    """
    return call_tool(
        calls,
        "check_invoice_history",
        _history_request(request),
        lambda: world.check_history(
            vendor_id=request.vendor_id,
            invoice_ref=request.invoice_ref,
            invoice_date=request.invoice_date,
            currency=request.currency,
            gross_amount=request.amount,
            po_number=request.po_number,
        ),
    )


def _aud_rate(world: FinanceWorld, request: ProcessingRequest) -> Decimal | None:
    """Resolve the AUD conversion rate for the invoice date.

    Args:
        world: Simulated systems for this case.
        request: Invoice case.

    Returns:
        ``1`` for AUD, the fixture rate for another currency, or ``None``
        when no rate was loaded.
    """
    if request.currency == "AUD":
        return Decimal("1")
    if request.invoice_date is None:
        return None
    return world.rates.get((request.currency, request.invoice_date))


def _dump(value: Any) -> Any:
    """Dump a model, or return ``None`` when the tool failed.

    Args:
        value: Tool result or ``None``.

    Returns:
        JSON-ready data, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [item.model_dump(mode="json") for item in value]
    return value.model_dump(mode="json")


def gather(world: FinanceWorld, index: PolicyIndex, request: ProcessingRequest) -> dict:
    """Collect policy, vendor, purchase-order, and history evidence.

    Args:
        world: Simulated systems for this case.
        index: Ingested finance corpus.
        request: Invoice case.

    Returns:
        Evidence stored on the ADK session. ``tool_calls`` keeps every
        attempt in the order it happened.
    """
    calls: list[dict] = []
    indicators = scan_many([request.notes, *[item.text for item in request.attachments]])
    vendor, vendor_attempts, vendor_error = _lookup_vendor(world, request, calls)
    purchase_order, po_attempts, po_error = _lookup_purchase_order(world, request, calls)
    history, history_attempts, history_error = _lookup_history(world, request, calls)
    chunks = _retrieve_policy(
        index,
        request,
        calls,
        vendor=vendor,
        purchase_order=purchase_order,
        indicators=indicators,
    )
    supplier_hits = [chunk for chunk in chunks if chunk.trust is Trust.UNTRUSTED]
    rate = _aud_rate(world, request)
    return {
        "indicators": indicators,
        "chunks": _dump(chunks),
        "supplier_chunks": _dump(supplier_hits),
        "citations": citations_from_hits(chunks),
        "budget_exceeded": any(
            item.get("error") and item["error"]["code"] == ToolErrorCode.BUDGET_EXCEEDED.value for item in calls
        ),
        "vendor": _dump(vendor),
        "vendor_error": _dump(vendor_error),
        "vendor_attempts": vendor_attempts,
        "purchase_order": _dump(purchase_order),
        "po_error": _dump(po_error),
        "po_attempts": po_attempts,
        "history": [] if history is None else _dump(history),
        "history_error": _dump(history_error),
        "history_attempts": history_attempts,
        "aud_rate": None if rate is None else str(rate),
        "tool_calls": calls,
    }


def run_engine(request: ProcessingRequest, evidence: dict) -> EngineResult:
    """Apply the finance rules to evidence already collected.

    Args:
        request: Invoice case.
        evidence: Payload returned by ``gather``.

    Returns:
        The deterministic recommendation and the facts behind it.
    """
    vendor = VendorRecord.model_validate(evidence["vendor"]) if evidence["vendor"] else None
    vendor_error = ToolError.model_validate(evidence["vendor_error"]) if evidence["vendor_error"] else None
    purchase_order = PurchaseOrder.model_validate(evidence["purchase_order"]) if evidence["purchase_order"] else None
    po_error = ToolError.model_validate(evidence["po_error"]) if evidence["po_error"] else None
    history = [InvoiceHistoryMatch.model_validate(item) for item in evidence["history"]]
    rate = Decimal(evidence["aud_rate"]) if evidence["aud_rate"] else None
    return reconcile(
        invoice_ref=request.invoice_ref,
        vendor_name=request.vendor_name,
        amount=request.amount,
        currency=request.currency,
        invoice_date=request.invoice_date,
        requester_id=request.requester_id,
        receipter_id=request.receipter_id,
        vendor=vendor,
        vendor_error=vendor_error,
        purchase_order=purchase_order,
        po_error=po_error,
        history=history,
        indicators=list(evidence["indicators"]),
        citations=evidence["chunks"],
        aud_rate=rate,
        invoice_lines=list(request.lines),
        freight=request.freight,
        service_confirmed=request.service_confirmed,
        non_po_justification=request.non_po_justification,
    )


def to_run_result(
    run_id: str,
    request: ProcessingRequest,
    engine: EngineResult,
    tool_calls: list | None = None,
    events: list | None = None,
) -> RunResult:
    """Copy a rules-engine result into the API snapshot.

    Args:
        run_id: ADK session id.
        request: Invoice case.
        engine: Recommendation produced by ``run_engine``.
        tool_calls: Trace collected while gathering evidence.
        events: Audit events collected so far.

    Returns:
        The snapshot stored in session state and returned by the API.
        An approval view is attached only when a person still has to decide.
    """
    approval = None
    if engine.state is RunState.AWAITING_APPROVAL:
        approval = ApprovalView(
            approval_id=f"appr_{run_id}",
            required_roles=engine.required_roles,
            status="PENDING",
        )
    return RunResult(
        run_id=run_id,
        case_id=request.case_id,
        request=request,
        state=engine.state,
        recommendation=engine.recommendation,
        reason=engine.reason,
        sourced_facts=engine.facts,
        calculations=engine.calculations,
        inferences=engine.inferences,
        unknowns=engine.unknowns,
        policy_findings=engine.findings,
        exceptions=engine.exceptions,
        confidence=engine.confidence,
        next_action=engine.next_action,
        approval=approval,
        tool_calls=tool_calls or [],
        events=[AuditEvent.model_validate(item) for item in (events or [])],
    )
