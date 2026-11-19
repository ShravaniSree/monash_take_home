"""Simulated vendor, purchase-order, invoice-history, and ledger systems."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import date
from decimal import Decimal

from ap_agent.domain.engine import normalise_invoice_ref
from ap_agent.domain.models import (
    InvoiceHistoryMatch,
    LedgerDecision,
    MatchType,
    PurchaseOrder,
    SubmitDecision,
    SubmitResult,
    ToolError,
    ToolErrorCode,
    VendorRecord,
)


class ToolFault(Exception):
    """A simulated system refused the call or could not complete it."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        """Store the stable error code and the message returned to the caller.

        Args:
            code: Machine-readable tool error.
            message: Text safe to show in the tool trace.
        """
        super().__init__(message)
        self.code = code
        self.message = message


_Q2 = Decimal("0.01")


def _require_snapshot(decision: SubmitDecision, snapshot: dict | None) -> None:
    """Refuse a post that does not match the engine snapshot.

    Args:
        decision: Ledger payload.
        snapshot: Recommendation captured before the human vote.

    Raises:
        ToolFault: The snapshot is missing or a field disagrees.
    """
    if not snapshot or snapshot.get("recommendation") != "APPROVE_FOR_POSTING":
        raise ToolFault(ToolErrorCode.DENIED, "posting does not match the recommendation")
    for field in ("vendor_id", "invoice_ref", "currency"):
        if snapshot.get(field) != getattr(decision, field):
            raise ToolFault(ToolErrorCode.DENIED, f"{field} does not match the recommendation")
    snapshot_amount = Decimal(str(snapshot.get("amount"))).quantize(_Q2)
    if snapshot_amount != decision.amount.quantize(_Q2):
        raise ToolFault(ToolErrorCode.DENIED, "amount does not match the recommendation")


class FinanceWorld:
    """In-memory vendor, purchase-order, history, rate, and ledger store."""

    def __init__(self) -> None:
        """Start empty. ``from_tools`` loads one case into a new instance."""
        self.vendors: dict[str, VendorRecord] = {}
        self.purchase_orders: dict[str, PurchaseOrder] = {}
        self.history: list[dict] = []
        self.rates: dict[tuple[str, date], Decimal] = {}
        self.po_failures_remaining = 0
        self.po_failure_code = ToolErrorCode.TIMEOUT
        self.ledger: dict[str, SubmitResult] = {}
        self.ledger_bodies: dict[str, str] = {}
        self.on_commit: Callable | None = None

    @classmethod
    def from_tools(cls, tools: dict) -> FinanceWorld:
        """Load one case's mock rows.

        Args:
            tools: ``vendors``, ``purchase_orders``, ``invoice_history``,
                ``rates``, and the purchase-order failure counters.

        Returns:
            A world containing only those rows.
        """
        world = cls()
        for vendor in tools.get("vendors", []):
            record = VendorRecord.model_validate(vendor)
            world.vendors[record.vendor_id] = record
        for purchase_order in tools.get("purchase_orders", []):
            record = PurchaseOrder.model_validate(purchase_order)
            world.purchase_orders[record.po_number] = record
        for row in tools.get("invoice_history", []):
            invoice_date = row.get("invoice_date")
            world.history.append(
                {
                    "record_id": row["record_id"],
                    "vendor_id": row["vendor_id"],
                    "invoice_ref": row["invoice_ref"],
                    "status": row["status"],
                    "currency": row["currency"],
                    "gross_amount": Decimal(row["gross_amount"]),
                    "invoice_date": date.fromisoformat(invoice_date) if invoice_date else None,
                    "po_number": row.get("po_number"),
                    "attachment_sha256": row.get("attachment_sha256"),
                }
            )
        for rate in tools.get("rates", []):
            world.rates[(rate["currency"], date.fromisoformat(rate["date"]))] = Decimal(rate["rate"])
        world.po_failures_remaining = int(tools.get("po_failures_remaining", 0))
        world.po_failure_code = ToolErrorCode(tools.get("po_failure_code", "TIMEOUT"))
        return world

    def get_vendor(self, vendor_id: str) -> VendorRecord:
        """Return the vendor master row.

        Args:
            vendor_id: Vendor id from the invoice.

        Returns:
            The stored vendor.

        Raises:
            ToolFault: No row exists for ``vendor_id``.
        """
        record = self.vendors.get(vendor_id)
        if record is None:
            raise ToolFault(ToolErrorCode.NOT_FOUND, f"vendor {vendor_id} not found")
        return record

    def get_purchase_order(self, po_number: str) -> PurchaseOrder:
        """Return the purchase order, after any scripted failures.

        Args:
            po_number: Purchase-order number from the invoice.

        Returns:
            The stored purchase order.

        Raises:
            ToolFault: Scripted failures remain, or the number is unknown.
        """
        if self.po_failures_remaining > 0:
            self.po_failures_remaining -= 1
            raise ToolFault(self.po_failure_code, "purchase order service unavailable")
        record = self.purchase_orders.get(po_number)
        if record is None:
            raise ToolFault(ToolErrorCode.NOT_FOUND, f"purchase order {po_number} not found")
        return record

    def check_history(
        self,
        *,
        vendor_id: str,
        invoice_ref: str,
        invoice_date: date | None,
        currency: str,
        gross_amount: Decimal,
        po_number: str | None,
        attachment_sha256: str | None = None,
    ) -> list[InvoiceHistoryMatch]:
        """Find exact and fuzzy duplicates for one vendor.

        An exact match is the same normalised invoice number, currency,
        and gross amount. A fuzzy match needs the same number plus a
        close amount or a close date, a close amount on a close date,
        the same purchase order and a close amount, or the same
        attachment hash. The purchase order alone is not a match.

        Args:
            vendor_id: Vendor on the invoice.
            invoice_ref: Invoice number.
            invoice_date: Invoice date, when present.
            currency: Invoice currency.
            gross_amount: Invoice gross amount.
            po_number: Purchase-order number, when present.
            attachment_sha256: Hash of the supplier attachment, when present.

        Returns:
            Matching history rows. An empty list means no candidate.
        """
        needle = normalise_invoice_ref(invoice_ref)
        matches: list[InvoiceHistoryMatch] = []
        for row in self.history:
            if row["vendor_id"] != vendor_id:
                continue
            same_number = normalise_invoice_ref(row["invoice_ref"]) == needle
            same_money = row["currency"] == currency and row["gross_amount"] == gross_amount
            if same_number and same_money:
                match_type = MatchType.EXACT
            else:
                close_amount = row["currency"] == currency and row["gross_amount"] > 0 and abs(
                    row["gross_amount"] - gross_amount
                ) / row["gross_amount"] < Decimal("0.005")
                close_date = (
                    invoice_date is not None
                    and row["invoice_date"] is not None
                    and abs((invoice_date - row["invoice_date"]).days) <= 14
                )
                same_po = po_number is not None and row["po_number"] == po_number
                same_attachment = (
                    attachment_sha256 is not None
                    and row.get("attachment_sha256")
                    and row["attachment_sha256"] == attachment_sha256
                )
                probable = (
                    (same_number and (close_amount or close_date))
                    or (close_amount and close_date)
                    or (same_po and close_amount)
                    or bool(same_attachment)
                )
                if probable:
                    match_type = MatchType.FUZZY
                else:
                    continue
            matches.append(
                InvoiceHistoryMatch(
                    record_id=row["record_id"],
                    invoice_ref=row["invoice_ref"],
                    status=row["status"],
                    match_type=match_type,
                    currency=row["currency"],
                    gross_amount=row["gross_amount"],
                    invoice_date=row["invoice_date"],
                )
            )
        return matches

    def submit(
        self,
        decision: SubmitDecision,
        *,
        approved: bool,
        snapshot: dict | None = None,
    ) -> SubmitResult:
        """Post a decision once, and replay the same key without a second post.

        Args:
            decision: Ledger payload, including the idempotency key.
            approved: True only after the gate has accepted every required role.
            snapshot: Engine snapshot captured when the recommendation was
                made. A post is refused when this is missing or disagrees
                with the payload.

        Returns:
            The new ledger row, or the original row with ``replayed`` set.

        Raises:
            ToolFault: The call is not approved, the payload disagrees with
                the snapshot, or the key is reused with a different body.
        """
        if not approved:
            raise ToolFault(ToolErrorCode.DENIED, "posting requires an approved decision")
        if decision.decision is LedgerDecision.POST:
            _require_snapshot(decision, snapshot)
        body = decision.model_dump_json()
        existing = self.ledger.get(decision.idempotency_key)
        if existing is not None:
            if self.ledger_bodies[decision.idempotency_key] != body:
                raise ToolFault(ToolErrorCode.CONFLICT, "idempotency key reused with a different payload")
            return existing.model_copy(update={"replayed": True})
        result = SubmitResult(
            ledger_ref=f"LED-{len(self.ledger) + 1001}",
            decision=decision.decision,
            replayed=False,
        )
        self.ledger[decision.idempotency_key] = result
        self.ledger_bodies[decision.idempotency_key] = body
        if self.on_commit is not None:
            self.on_commit(decision.idempotency_key, result, body)
        return result


def idempotency_key(
    *,
    run_id: str,
    case_id: str,
    decision: LedgerDecision,
    vendor_id: str,
    invoice_ref: str,
    currency: str,
    amount: Decimal,
    approval_id: str,
) -> str:
    """Hash the decision fields that must stay stable for a replay.

    Args:
        run_id: Session id.
        case_id: Fixture or case id.
        decision: Ledger action.
        vendor_id: Vendor being paid.
        invoice_ref: Invoice number.
        currency: Payment currency.
        amount: Payment amount.
        approval_id: Approval that authorised the post.

    Returns:
        SHA-256 hex digest used as the idempotency key.
    """
    raw = "|".join(
        [run_id, case_id, decision.value, vendor_id, invoice_ref, currency, format(amount, "f"), approval_id]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
