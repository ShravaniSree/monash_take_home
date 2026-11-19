"""Run the ADK workflow and resume the same session for an approval."""

from __future__ import annotations

import asyncio
from pathlib import Path

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.sqlite_session_service import SqliteSessionService
from google.genai import types

from ap_agent.application.app import APP_NAME
from ap_agent.application.app import USER_ID
from ap_agent.application.app import bind_deps
from ap_agent.application.app import build_app
from ap_agent.application.app import result_from_state
from ap_agent.domain.models import ApprovalDecision
from ap_agent.domain.models import AuditEvent
from ap_agent.domain.models import ProcessingRequest
from ap_agent.domain.models import RunResult
from ap_agent.domain.models import RunState
from ap_agent.domain.redact import redact_request
from ap_agent.utils.paths import corpus_dir
from ap_agent.retrieval.index import PolicyIndex
from ap_agent.systems.fixtures import load_world
from ap_agent.systems.store import AuditStore
from ap_agent.systems.world import FinanceWorld

__all__ = ["ApService", "DecisionConflict", "build_service", "corpus_dir"]


class DecisionConflict(Exception):
    """A decision arrived for a run that is no longer waiting for one."""

    def __init__(self, result: RunResult) -> None:
        """Keep the stored snapshot so the API can return it unchanged.

        Args:
            result: The run as it was completed or failed.
        """
        super().__init__(result.run_id)
        self.result = result


class ApService:
    """One process-wide runner, with a separate simulated world per run."""

    def __init__(
        self,
        index: PolicyIndex,
        world: FinanceWorld,
        sessions: InMemorySessionService | None = None,
        store: AuditStore | None = None,
    ) -> None:
        """Store the shared policy index and the session backend.

        Args:
            index: Ingested finance corpus. Shared by every run.
            world: Unused placeholder. ``start`` replaces it with the
                case world loaded from fixtures.
            sessions: ADK session store. Tests pass an in-memory store.
                The API and CLI pass SQLite.
            store: Durable ledger and audit log. A file store lets a new
                process replay a post instead of creating another one.
        """
        self.index = index
        self.world = world
        self.sessions = sessions or InMemorySessionService()
        self.store = store or AuditStore()
        self.runner = Runner(app=build_app(), session_service=self.sessions, auto_create_session=False)
        self.worlds: dict[str, FinanceWorld] = {}
        self._lock = asyncio.Lock()

    def _attach(self, world: FinanceWorld) -> FinanceWorld:
        """Copy the durable ledger onto a world and save new posts back.

        Args:
            world: Simulated systems for one case.

        Returns:
            The same world, with ledger rows loaded from the store.
        """
        for key, result, body in self.store.load_decisions():
            world.ledger.setdefault(key, result)
            world.ledger_bodies.setdefault(key, body)
        world.on_commit = self.store.save_decision
        return world

    async def start(self, request: ProcessingRequest) -> RunResult:
        """Create a session and run the workflow until it pauses or finishes.

        Args:
            request: Invoice case. Tool mocks are loaded from ``request.case_id``.

        Returns:
            The run snapshot, including the tool-call trace.

        Raises:
            FileNotFoundError: No fixture exists for the case id.
        """
        world = self._attach(load_world(request.case_id))
        stored = redact_request(request)
        async with self._lock:
            self.world = world
            bind_deps(self.world, self.index, self.store)
            session = await self.sessions.create_session(
                app_name=APP_NAME,
                user_id=USER_ID,
                state={"request": stored.model_dump(mode="json"), "submit_count": 0},
            )
            self.worlds[session.id] = world
            await self._drain(
                session.id,
                types.Content(role="user", parts=[types.Part(text=request.case_id)]),
            )
            return await self.get(session.id)

    async def decide(self, run_id: str, decision: ApprovalDecision) -> RunResult:
        """Resume a waiting run with an approval or rejection.

        Args:
            run_id: Session id returned by ``start``.
            decision: Human decision. The role is checked against the
                amount before anything is posted.

        Returns:
            The updated run snapshot.

        Raises:
            KeyError: No session exists for ``run_id``.
            DecisionConflict: The run is not waiting, and this vote is not
                a repeat of the vote that closed it.
        """
        async with self._lock:
            session = await self.sessions.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=run_id)
            if session is None:
                raise KeyError(run_id)
            current = result_from_state(session.state)
            if current.state is not RunState.AWAITING_APPROVAL:
                if _same_decision(session.state.get("resolved_decision"), decision):
                    self.store.save_event(
                        AuditEvent(
                            ts=_clock(),
                            run_id=run_id,
                            type="approval.duplicate",
                            outcome="replayed",
                            detail={},
                        )
                    )
                    return current
                raise DecisionConflict(current)
            world = self.worlds.get(run_id)
            if world is None:
                request = ProcessingRequest.model_validate(session.state["request"])
                world = self._attach(load_world(request.case_id))
                self.worlds[run_id] = world
            self.world = world
            bind_deps(self.world, self.index, self.store)
            await self._drain(
                run_id,
                types.Content(role="user", parts=[types.Part(text="approval")]),
                {"approval_decision": decision.model_dump(mode="json")},
            )
            return await self.get(run_id)

    async def get(self, run_id: str) -> RunResult:
        """Read the stored snapshot for a run.

        Args:
            run_id: Session id.

        Returns:
            The last ``RunResult`` written into session state.

        Raises:
            KeyError: The session does not exist.
        """
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=USER_ID, session_id=run_id)
        if session is None:
            raise KeyError(run_id)
        return result_from_state(session.state)

    async def _drain(self, session_id: str, message: types.Content, state_delta: dict | None = None) -> None:
        """Run the workflow to the next pause and discard the event stream.

        Args:
            session_id: Existing ADK session.
            message: User turn that wakes the runner.
            state_delta: Optional state written before the agents run.
                Approvals are passed here.
        """
        async for _event in self.runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=message,
            state_delta=state_delta,
        ):
            pass


def build_service(root: Path) -> ApService:
    """Build the service used by the API and the CLI.

    Args:
        root: Directory that will hold ``data/policy.sqlite`` and the
            session database. It is created when missing.

    Returns:
        A service with the corpus ingested and sessions stored in SQLite.
    """
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    index = PolicyIndex(data / "policy.sqlite")
    index.ingest_dir(corpus_dir())
    sessions = SqliteSessionService(str(data / "adk_sessions.sqlite"))
    store = AuditStore(data / "ap_agent.sqlite")
    return ApService(index, FinanceWorld(), sessions, store)


def _same_decision(stored: dict | None, decision: ApprovalDecision) -> bool:
    """Return True when this vote repeats the one that closed the run.

    Args:
        stored: Decision saved when the run left ``AWAITING_APPROVAL``.
        decision: Vote just received.

    Returns:
        True when the action, approver, and role all match.
    """
    if not stored:
        return False
    return (
        stored.get("action") == decision.action.value
        and stored.get("approver_id") == decision.approver_id
        and stored.get("role") == decision.role
    )


def _clock() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
