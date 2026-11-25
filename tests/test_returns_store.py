from app.returns.engine import build_ledger
from app.returns.store import ReturnsStore


SALE = {"sale_id": "S1", "sale_date": "2026-11-01", "line_qty": 10,
        "net_revenue_cents": 9000, "net_cost_cents": 5000}


def refund(credential="RF1", qty=4, day="2026-11-05", amount=3600):
    return {"credential_id": credential, "kind": "refund", "sale_id": "S1", "qty": qty,
            "received_date": day, "amount_cents": amount}


def acceptance(credential="AC1", qty=4, day="2026-11-03"):
    return {"credential_id": credential, "kind": "acceptance", "sale_id": "S1", "qty": qty,
            "accepted_date": day}


def store(tmp_path):
    return ReturnsStore(str(tmp_path / "returns.sqlite3"))


def test_sales_are_idempotent(tmp_path):
    db = store(tmp_path)
    assert db.ingest_sales([SALE])["ingested"] == 1
    assert db.ingest_sales([SALE])["replayed"] == 1
    assert len(db.sales()) == 1


def test_conflicting_sale_keeps_first(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    changed = dict(SALE, line_qty=99)
    result = db.ingest_sales([changed])
    assert result["conflicts"] == 1
    assert db.sales()[0]["line_qty"] == 10


def test_documents_are_idempotent(tmp_path):
    db = store(tmp_path)
    db.ingest_documents([refund()])
    assert db.ingest_documents([refund()])["replayed"] == 1
    assert len(db.documents()) == 1


def test_out_of_order_arrival_recomputes_timeline(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    db.ingest_documents([refund()])
    first = build_ledger(sales=db.sales(), documents=db.documents())
    assert first["sales"][0]["cost_reversal_cents"] == 0
    assert first["sales"][0]["revenue_reversal_cents"] == 3600
    db.ingest_documents([acceptance()])
    second = build_ledger(sales=db.sales(), documents=db.documents())
    assert second["sales"][0]["cost_reversal_cents"] == 2000
    # 验收早于退款，成本在退款到账当天才冲回。
    cost_day = next(row for row in second["days"] if row["date"] == "2026-11-05")
    assert cost_day["cost_reversal_cents"] == 2000


def test_late_acceptance_books_cost_on_acceptance_day(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    db.ingest_documents([refund(day="2026-11-02")])
    db.ingest_documents([acceptance(day="2026-11-04")])
    result = build_ledger(sales=db.sales(), documents=db.documents())
    day = next(row for row in result["days"] if row["date"] == "2026-11-04")
    assert day["cost_reversal_cents"] == 2000
    assert day["revenue_reversal_cents"] == 0


def test_void_arrives_before_acceptance(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    db.ingest_documents([refund(),
                         {"credential_id": "V1", "kind": "void", "target_doc_id": "AC1",
                          "voided_date": "2026-11-04"}])
    assert build_ledger(sales=db.sales(), documents=db.documents())["sales"][0][
        "cost_reversal_cents"] == 0
    db.ingest_documents([acceptance()])
    result = build_ledger(sales=db.sales(), documents=db.documents())
    assert result["sales"][0]["cost_reversal_cents"] == 0
    assert result["sales"][0]["revenue_reversal_cents"] == 3600


def test_acceptance_can_be_voided_in_place(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    db.ingest_documents([refund(), acceptance()])
    result = db.ingest_documents([{"credential_id": "AC1", "kind": "acceptance", "sale_id": "S1",
                                   "qty": 4, "status": "voided", "voided_date": "2026-11-06"}])
    assert result["updated"] == 1
    stored = db.documents(kind="acceptance")[0]
    assert stored["accepted_date"] == "2026-11-03"
    ledger = build_ledger(sales=db.sales(), documents=db.documents())
    assert ledger["sales"][0]["cost_reversal_cents"] == 0
    assert ledger["sales"][0]["revenue_reversal_cents"] == 3600


def test_document_without_credential_is_isolated(tmp_path):
    db = store(tmp_path)
    result = db.ingest_documents([{"sale_id": "S1", "qty": 1, "kind": "refund",
                                   "received_date": "2026-11-03", "amount_cents": 100}])
    assert result["isolated"] == 1
    assert db.documents() == []


def test_clear_removes_everything(tmp_path):
    db = store(tmp_path)
    db.ingest_sales([SALE])
    db.ingest_documents([refund()])
    db.clear()
    assert db.snapshot() == {"sales": [], "documents": []}
