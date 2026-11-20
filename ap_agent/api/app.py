"""HTTP API for the accounts-payable workflow."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from pydantic import BaseModel
from pydantic import Field

from ap_agent.application.service import DecisionConflict
from ap_agent.application.service import build_service
from ap_agent.domain.models import ApprovalDecision
from ap_agent.domain.models import ProcessingRequest
from ap_agent.domain.models import Recommendation
from ap_agent.domain.models import RunResult
from ap_agent.domain.models import RunState
from ap_agent.utils.env import load_local_env
from ap_agent.utils.logging import get_logger
from ap_agent.utils.logging import log_event
from ap_agent.utils.logging import request_id
from ap_agent.utils.paths import scenarios_dir
from ap_agent.systems.fixtures import load_scenario
from ap_agent.systems.fixtures import request_from_payload

_EXPECTED: dict[str, Recommendation] = {
    "FIN-001": Recommendation.APPROVE_FOR_POSTING,
    "FIN-002": Recommendation.REJECT_DUPLICATE,
    "FIN-003": Recommendation.ESCALATE_CONTROL_REVIEW,
    "FIN-004": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-005": Recommendation.APPROVE_FOR_POSTING,
    "FIN-006": Recommendation.ESCALATE_CONTROL_REVIEW,
    "FIN-007": Recommendation.APPROVE_FOR_POSTING,
    "FIN-008": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-009": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-010": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-011": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-012": Recommendation.APPROVE_FOR_POSTING,
    "FIN-013": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-014": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-015": Recommendation.ESCALATE_CONTROL_REVIEW,
    "FIN-016": Recommendation.HOLD_FOR_INFORMATION,
    "FIN-017": Recommendation.REJECT_INVALID,
}


class EvaluationCase(BaseModel):
    """One fixture compared with the recommendation the engine produced."""

    case_id: str
    passed: bool
    recommendation: str
    expected: str


class EvaluationReport(BaseModel):
    """Pass and fail counts for a full fixture run."""

    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cases: list[EvaluationCase]


class ErrorBody(BaseModel):
    """Error payload documented on the OpenAPI responses."""

    detail: str


class ScenarioInput(BaseModel):
    """Scenario document. Tool mocks are loaded separately by ``case_id``."""

    case_id: str
    description: str = ""
    request: ProcessingRequest


def _no_payment(result: RunResult) -> bool:
    """Return True when the run did not post and did not ask for payment."""
    return result.approval is None and result.actions_taken == [] and result.state is not RunState.AWAITING_APPROVAL


async def _controls_passed(service: object, case_id: str, result: RunResult) -> tuple[bool, str]:
    """Check the control the case exists to prove, not only the outcome code.

    Args:
        service: Running service, used to deliver the duplicate approval.
        case_id: Fixture id.
        result: Snapshot from the start call.

    Returns:
        Whether the control held, and a short label of what was expected.
    """
    expected = _EXPECTED.get(case_id)
    if expected is None or result.recommendation is not expected:
        return False, expected.value if expected else "UNSET"
    if case_id in {"FIN-001", "FIN-007"}:
        ok = result.state is RunState.AWAITING_APPROVAL and result.approval is not None and result.actions_taken == []
        return ok, "APPROVE_FOR_POSTING and wait"
    if case_id == "FIN-005":
        decision = ApprovalDecision(
            action="APPROVED",
            approver_id="U-DIR-1",
            role="DEPARTMENT_DIRECTOR",
            comment="Reviewed",
        )
        first = await service.decide(result.run_id, decision)
        second = await service.decide(result.run_id, decision)
        ok = (
            result.state is RunState.AWAITING_APPROVAL
            and len(first.actions_taken) == 1
            and len(second.actions_taken) == 1
            and second.actions_taken[0].ledger_ref == first.actions_taken[0].ledger_ref
        )
        return ok, "one post, second decision replays it"
    if case_id == "FIN-004":
        attempts = [call for call in result.tool_calls if call.tool == "get_purchase_order"]
        ok = _no_payment(result) and len(attempts) == 3 and attempts[-1].error is not None
        return ok, "HOLD_FOR_INFORMATION after three timeouts"
    if case_id == "FIN-012":
        ok = (
            result.state is RunState.AWAITING_APPROVAL
            and result.approval is not None
            and result.approval.required_roles == ["DEPARTMENT_DIRECTOR", "FINANCIAL_CONTROL"]
            and result.actions_taken == []
        )
        return ok, "APPROVE_FOR_POSTING with Financial Control"
    if case_id in {"FIN-002", "FIN-003", "FIN-006", "FIN-008", "FIN-009", "FIN-010", "FIN-011", "FIN-013", "FIN-014", "FIN-015", "FIN-016", "FIN-017"}:
        return _no_payment(result), f"{expected.value} and no payment"
    return _no_payment(result) or result.state is RunState.AWAITING_APPROVAL, expected.value


def create_app(root: Path | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        root: Directory for the policy and session databases. Defaults to
            the process working directory.

    Returns:
        An application whose lifespan constructs ``ApService``.
    """
    data_root = root or Path.cwd()
    load_local_env(data_root)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Ingest the corpus and keep the service on application state."""
        app.state.service = build_service(data_root)
        yield

    app = FastAPI(
        title="Northstar Accounts Payable Agent",
        version="0.1.0",
        summary="Process an invoice case, ground it in policy, and stop for approval before posting.",
        description=(
            "Vendor, purchase-order, invoice-history, and rate mocks are loaded by case_id from "
            "fixtures/tools. The policy index is the ingested finance corpus. "
            "A recommendation of APPROVE_FOR_POSTING does not post until POST /runs/{run_id}/decision."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def log_http(request, call_next):
        """Attach a request id and log the status and duration."""
        incoming = request.headers.get("x-request-id") or str(uuid4())
        token = request_id.set(incoming)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = incoming
            return response
        finally:
            log_event(
                get_logger(__name__),
                "http_request",
                request_id=incoming,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            request_id.reset(token)

    @app.get("/", include_in_schema=False)
    def tool_trace_page() -> HTMLResponse:
        """Return the page that shows each tool call beside its response."""
        page = Path(__file__).with_name("ui.html").read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        """Report that the process is accepting requests."""
        return {"status": "ok"}

    @app.get("/scenarios", tags=["meta"], summary="List scenario requests")
    def list_scenarios() -> list[dict]:
        """List fixture ids and the description stored with each request."""
        items = []
        for path in sorted(scenarios_dir().glob("FIN-*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            items.append({"case_id": path.stem, "description": payload.get("description", "")})
        return items

    @app.get("/scenarios/{case_id}", tags=["meta"], summary="Read one scenario request")
    def read_scenario(case_id: str) -> dict:
        """Return one scenario document, ready to post to ``/runs``."""
        path = scenarios_dir() / f"{case_id}.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"Scenario {case_id} was not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.post(
        "/runs",
        response_model=RunResult,
        tags=["runs"],
        responses={404: {"model": ErrorBody}, 422: {"model": ErrorBody}},
        summary="Start a run",
    )
    async def start_run(body: ProcessingRequest | ScenarioInput) -> RunResult:
        """Start a run from a bare request or a scenario document."""
        payload = body.model_dump(mode="json")
        try:
            request = request_from_payload(payload)
            return await app.state.service.start(request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc

    @app.get(
        "/runs/{run_id}",
        response_model=RunResult,
        tags=["runs"],
        responses={404: {"model": ErrorBody}},
        summary="Get a run",
    )
    async def get_run(run_id: str) -> RunResult:
        """Return the stored snapshot, including the tool-call trace."""
        try:
            return await app.state.service.get(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Run {run_id} was not found") from exc

    @app.post(
        "/runs/{run_id}/decision",
        response_model=RunResult,
        tags=["runs"],
        responses={404: {"model": ErrorBody}, 409: {"model": RunResult}, 422: {"model": ErrorBody}},
        summary="Approve or reject a pending run",
    )
    async def decide_run(run_id: str, decision: ApprovalDecision) -> RunResult:
        """Resume a waiting run and post only when every required role has approved."""
        try:
            return await app.state.service.decide(run_id, decision)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Run {run_id} was not found") from exc
        except DecisionConflict as exc:
            return JSONResponse(status_code=409, content=exc.result.model_dump(mode="json"))

    @app.post(
        "/evaluations",
        response_model=EvaluationReport,
        tags=["evaluations"],
        summary="Run the scenario fixtures and report pass or fail",
    )
    async def evaluate() -> EvaluationReport:
        """Run every FIN fixture and compare its recommendation with the expected one."""
        cases: list[EvaluationCase] = []
        for path in sorted(scenarios_dir().glob("FIN-*.json")):
            case_id = path.stem
            request, _world = load_scenario(case_id)
            result = await app.state.service.start(request)
            passed, expected_label = await _controls_passed(app.state.service, case_id, result)
            cases.append(
                EvaluationCase(
                    case_id=case_id,
                    passed=passed,
                    recommendation=result.recommendation.value,
                    expected=expected_label,
                )
            )
        passed = sum(1 for item in cases if item.passed)
        return EvaluationReport(passed=passed, failed=len(cases) - passed, cases=cases)

    return app
