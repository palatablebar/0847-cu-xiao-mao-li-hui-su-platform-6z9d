import json

from app.returns import service as returns


def payload():
    with open("sample_data/returns.json", encoding="utf-8") as stream:
        return json.load(stream)


def test_ledger_endpoint_returns_days(client):
    response = client.post("/api/returns/ledger", json=payload())
    assert response.status_code == 200
    data = response.get_json()
    assert data["schema"] == "workbench-returns/v1"
    assert data["days"]
    assert data["totals"]["net_margin_cents"] == sum(
        row["net_margin_cents"] for row in data["days"])


def test_day_endpoint_selects_net_margin(client):
    response = client.get("/api/returns/day?date=2026-11-05")
    assert response.status_code == 200
    assert response.get_json()["date"] == "2026-11-05"
    assert response.get_json()["net_margin_cents"] == -1600


def test_day_endpoint_requires_date(client):
    assert client.get("/api/returns/day").status_code == 400


def test_day_endpoint_accepts_post_body(client):
    data = payload()
    data["date"] = "2026-11-07"
    response = client.post("/api/returns/day", json=data)
    assert response.status_code == 200
    assert response.get_json()["date"] == "2026-11-07"
    assert response.get_json()["net_margin_cents"] == 2400


def test_bad_payload_is_rejected(client):
    assert client.post("/api/returns/ledger", json={"sales": "oops"}).status_code == 400


def test_return_run_is_idempotent(client):
    first = client.post("/api/returns/runs", json=payload()).get_json()
    reversed_docs = payload()
    reversed_docs["documents"] = list(reversed(reversed_docs["documents"]))
    second = client.post("/api/returns/runs", json=reversed_docs).get_json()
    assert first["run_id"] == second["run_id"]
    assert first["returns"]["totals"] == second["returns"]["totals"]


def test_return_run_can_be_approved_and_evidenced(client):
    run = client.post("/api/returns/runs", json=payload()).get_json()
    review = client.post(f"/api/runs/{run['run_id']}/review",
                         json={"decision": "APPROVED", "comment": "退货复核", "expected_revision": 0})
    assert review.status_code == 200
    evidence = client.get(f"/api/runs/{run['run_id']}/evidence")
    assert evidence.status_code == 200
    artifact = evidence.get_json()
    assert artifact["review"]["status"] == "APPROVED"
    assert artifact["ledger"]["total_net_margin_cents"] == run["returns"]["totals"]["net_margin_cents"]


def test_request_key_ignores_document_order():
    first = payload()
    second = payload()
    second["documents"] = list(reversed(second["documents"]))
    assert returns.request_key(first) == returns.request_key(second)


def test_request_key_changes_with_amount():
    changed = payload()
    changed["documents"][0]["amount_cents"] += 1
    assert returns.request_key(payload()) != returns.request_key(changed)


def test_profile_sales_become_return_lines(client):
    data = {"profile": "clean", "sale_date": "2026-11-01",
            "documents": [{"credential_id": "R", "kind": "refund", "sale_id": "A",
                           "qty": 1, "received_date": "2026-11-02", "amount_cents": 100}]}
    result = client.post("/api/returns/ledger", json=data).get_json()
    assert len(result["sales"]) == 3
    line = next(item for item in result["sales"] if item["sale_id"] == "A")
    assert line["revenue_reversal_cents"] == 100


def test_glossary_lists_returns_endpoints(client):
    catalog = client.get("/api/catalog").get_json()
    assert "returns" in catalog["samples"]


def test_returns_example_downloads(client):
    response = client.get("/api/examples/returns")
    assert response.status_code == 200
    assert response.mimetype == "application/json"


def test_stored_ledger_uses_ingested_documents(client):
    assert client.delete("/api/returns/state").status_code == 200
    ingest = client.post("/api/returns/ingest", json={"profile": "returns"}).get_json()
    assert ingest["documents"]["ingested"] == 6
    ledger = client.post("/api/returns/ledger",
                         json={"stored": True, "date": "2026-11-05"}).get_json()
    assert ledger["selected_day"]["net_margin_cents"] == -1600


def test_ingest_is_idempotent(client):
    client.delete("/api/returns/state")
    client.post("/api/returns/ingest", json={"profile": "returns"})
    again = client.post("/api/returns/ingest", json={"profile": "returns"}).get_json()
    assert again["documents"]["ingested"] == 0
    assert again["documents"]["replayed"] == 6


def test_late_acceptance_updates_stored_ledger(client):
    client.delete("/api/returns/state")
    client.post("/api/returns/ingest",
                json={"sales": [{"sale_id": "S1", "sale_date": "2026-11-01", "line_qty": 10,
                                 "net_revenue_cents": 9000, "net_cost_cents": 5000}],
                      "documents": [{"credential_id": "RF1", "kind": "refund", "sale_id": "S1",
                                     "qty": 4, "received_date": "2026-11-05",
                                     "amount_cents": 3600}]})
    before = client.get("/api/returns/state").get_json()["ledger"]
    assert before["sales"][0]["cost_reversal_cents"] == 0
    client.post("/api/returns/ingest",
                json={"documents": [{"credential_id": "AC1", "kind": "acceptance",
                                     "sale_id": "S1", "qty": 4,
                                     "accepted_date": "2026-11-07"}]})
    after = client.get("/api/returns/state").get_json()["ledger"]
    assert after["sales"][0]["cost_reversal_cents"] == 2000
    assert after["sales"][0]["revenue_reversal_cents"] == 3600
    cost_day = next(row for row in after["days"] if row["date"] == "2026-11-07")
    assert cost_day["cost_reversal_cents"] == 2000


def test_stored_run_is_idempotent(client):
    client.delete("/api/returns/state")
    client.post("/api/returns/ingest", json={"profile": "returns"})
    first = client.post("/api/returns/runs", json={"stored": True}).get_json()
    second = client.post("/api/returns/runs", json={"stored": True}).get_json()
    assert first["run_id"] == second["run_id"]


def test_void_state_can_be_reset(client):
    client.delete("/api/returns/state")
    client.post("/api/returns/ingest", json={"profile": "returns"})
    client.delete("/api/returns/state")
    state = client.get("/api/returns/state").get_json()
    assert state["sales"] == []
    assert state["documents"] == []
