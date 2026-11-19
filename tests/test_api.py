import json
from pathlib import Path

from fastapi.testclient import TestClient

from ap_agent.api import create_app
from ap_agent.systems.fixtures import load_scenario


def _client(tmp_path):
    return TestClient(create_app(tmp_path))


def test_scenario_file_is_a_valid_start_body(tmp_path):
    document = json.loads(
        (Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "FIN-002.json").read_text(encoding="utf-8")
    )
    with _client(tmp_path) as client:
        started = client.post("/runs", json=document)
    assert started.status_code == 200
    assert started.json()["recommendation"] == "REJECT_DUPLICATE"


def test_start_get_and_reject_duplicate(tmp_path):
    request, _world = load_scenario("FIN-002")
    with _client(tmp_path) as client:
        started = client.post("/runs", json=request.model_dump(mode="json"))
        assert started.status_code == 200
        body = started.json()
        assert body["recommendation"] == "REJECT_DUPLICATE"
        assert "duplicate.exact" in body["reason"]
        assert body["actions_taken"] == []

        fetched = client.get(f"/runs/{body['run_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["case_id"] == "FIN-002"


def test_approval_posts_once(tmp_path):
    request, _world = load_scenario("FIN-001")
    decision = {"action": "APPROVED", "approver_id": "U-DIR-1", "role": "DEPARTMENT_DIRECTOR", "comment": "Reviewed"}
    with _client(tmp_path) as client:
        started = client.post("/runs", json=request.model_dump(mode="json"))
        run_id = started.json()["run_id"]
        assert started.json()["state"] == "AWAITING_APPROVAL"

        posted = client.post(f"/runs/{run_id}/decision", json=decision)
        assert posted.status_code == 200
        assert posted.json()["state"] == "COMPLETED"
        assert len(posted.json()["actions_taken"]) == 1

        replayed = client.post(f"/runs/{run_id}/decision", json=decision)
        assert replayed.status_code == 200
        assert replayed.json()["actions_taken"][0]["ledger_ref"] == posted.json()["actions_taken"][0]["ledger_ref"]
        conflict = client.post(
            f"/runs/{run_id}/decision",
            json={"action": "REJECTED", "approver_id": "U-DIR-1", "role": "DEPARTMENT_DIRECTOR"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["actions_taken"][0]["ledger_ref"] == posted.json()["actions_taken"][0]["ledger_ref"]


def test_unknown_case_and_run(tmp_path):
    request, _world = load_scenario("FIN-001")
    payload = request.model_dump(mode="json")
    payload["case_id"] = "FIN-999"
    with _client(tmp_path) as client:
        missing_case = client.post("/runs", json=payload)
        assert missing_case.status_code == 404
        missing_run = client.get("/runs/missing")
        assert missing_run.status_code == 404
        invalid = client.post("/runs", json={"case_id": "FIN-001"})
        assert invalid.status_code == 422


def test_tool_trace_pairs_each_call_with_its_response(tmp_path):
    request, _world = load_scenario("FIN-004")
    decision = {"action": "APPROVED", "approver_id": "U-DIR-1", "role": "DEPARTMENT_DIRECTOR", "comment": "Reviewed"}
    with _client(tmp_path) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Tool call trace" in page.text
        assert "Initial request" in page.text
        started = client.post("/runs", json=request.model_dump(mode="json"))
        assert started.json()["request"]["invoice_ref"] == request.invoice_ref
        assert "LLM prompt" in page.text
        assert "Final response" in page.text
        assert started.json()["llm_prompt"]["system_instruction"]
        assert started.json()["llm_response"]
        assert "purchase order unavailable (TIMEOUT)" in started.json()["reason"]
        calls = started.json()["tool_calls"]
        po_calls = [call for call in calls if call["tool"] == "get_purchase_order"]
        assert len(po_calls) == 3
        assert po_calls[0]["request"]["po_number"] == request.po_number
        assert po_calls[-1]["error"]["code"] == "TIMEOUT"
        vendor = next(call for call in calls if call["tool"] == "get_vendor_record")
        assert vendor["response"]["vendor_id"] == request.vendor_id
        approved = client.post("/runs", json=load_scenario("FIN-001")[0].model_dump(mode="json"))
        run_id = approved.json()["run_id"]
        posted = client.post(f"/runs/{run_id}/decision", json=decision)
        submit = [call for call in posted.json()["tool_calls"] if call["tool"] == "submit_finance_decision"]
        assert len(submit) == 1
        assert submit[0]["response"]["ledger_ref"]


def test_openapi_lists_the_workflow(tmp_path):
    with _client(tmp_path) as client:
        spec = client.get("/openapi.json")
    assert spec.status_code == 200
    paths = spec.json()["paths"]
    assert "/runs" in paths
    assert "/runs/{run_id}" in paths
    assert "/runs/{run_id}/decision" in paths
    assert "/evaluations" in paths
