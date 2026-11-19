"""Load a case request, and load that case's tool mocks by case id."""

from __future__ import annotations

import json
from pathlib import Path

from ap_agent.domain.models import ProcessingRequest
from ap_agent.utils.paths import scenarios_dir
from ap_agent.utils.paths import tools_dir
from ap_agent.systems.world import FinanceWorld


def scenario_path(case_id: str) -> Path:
    """Return the scenario file for one case.

    Args:
        case_id: Fixture id such as ``FIN-001``.

    Returns:
        Path of ``fixtures/scenarios/{case_id}.json``. The file is not
        required to exist yet.
    """
    return scenarios_dir() / f"{case_id}.json"


def _read_tool_file(name: str) -> dict:
    """Read one tool mock table.

    Args:
        name: File name inside ``fixtures/tools``.

    Returns:
        Parsed JSON object keyed by case id.

    Raises:
        FileNotFoundError: The mock file is not on disk.
    """
    path = tools_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"Tool mock file is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_world(case_id: str) -> FinanceWorld:
    """Build the simulated systems for one case.

    Args:
        case_id: Fixture id used to select rows from each tool file.

    Returns:
        A world whose vendor, purchase-order, history, and rate tables
        contain only that case.

    Raises:
        FileNotFoundError: A tool file has no entry for ``case_id``.
    """
    vendors = _read_tool_file("vendors.json")
    orders = _read_tool_file("purchase_orders.json")
    history = _read_tool_file("invoice_history.json")
    rates = _read_tool_file("rates.json")
    missing = [name for name, table in (
        ("vendors.json", vendors),
        ("purchase_orders.json", orders),
        ("invoice_history.json", history),
        ("rates.json", rates),
    ) if case_id not in table]
    if missing:
        raise FileNotFoundError(f"No tool mock for {case_id} in {', '.join(missing)}")
    purchase_orders = orders[case_id]
    return FinanceWorld.from_tools(
        {
            "vendors": vendors[case_id],
            "purchase_orders": purchase_orders["records"],
            "invoice_history": history[case_id],
            "rates": rates[case_id],
            "po_failures_remaining": purchase_orders["failures_remaining"],
            "po_failure_code": purchase_orders["failure_code"],
        }
    )


def load_scenario(case_id: str) -> tuple[ProcessingRequest, FinanceWorld]:
    """Load a case request and the tool world that belongs to it.

    Args:
        case_id: Fixture id such as ``FIN-001``.

    Returns:
        The validated request and a freshly loaded world.

    Raises:
        FileNotFoundError: No scenario file exists for ``case_id``.
    """
    path = scenario_path(case_id)
    if not path.is_file():
        known = sorted(item.stem for item in scenarios_dir().glob("*.json"))
        raise FileNotFoundError(
            f"No scenario for case {case_id}. Expected {path}. Known cases: {', '.join(known)}"
        )
    request = request_from_payload(json.loads(path.read_text(encoding="utf-8")))
    return request, load_world(request.case_id)


def request_from_payload(payload: dict) -> ProcessingRequest:
    """Accept a bare invoice request or a scenario file.

    Tool mocks are not read from the file. They are loaded later by
    ``case_id``.

    Args:
        payload: Either a ``ProcessingRequest`` object or a scenario
            document with a ``request`` object.

    Returns:
        The validated processing request.
    """
    if isinstance(payload.get("request"), dict):
        return ProcessingRequest.model_validate(payload["request"])
    return ProcessingRequest.model_validate(payload)
