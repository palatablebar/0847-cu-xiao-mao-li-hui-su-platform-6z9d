import json

import pytest

from app.returns.engine import build_ledger, document_kind
from app.returns.service import (ReturnsInputError, compute, day_view, load_sample,
                                 prepare, request_key, summarize)

SALE = {"sale_id": "S1", "sale_date": "2026-11-01", "line_qty": 10,
        "net_revenue_cents": 9000, "net_cost_cents": 5000}


def refund(credential="RF1", qty=4, day="2026-11-05", amount=3600):
    return {"credential_id": credential, "kind": "refund", "sale_id": "S1", "qty": qty,
            "received_date": day, "amount_cents": amount}


def acceptance(credential="AC1", qty=4, day="2026-11-03"):
    return {"credential_id": credential, "kind": "acceptance", "sale_id": "S1", "qty": qty,
            "accepted_date": day}


def test_sample_file_is_loadable():
    sample = load_sample("returns")
    result = compute(sample)
    assert result["schema"] == "workbench-returns/v1"
    assert len(result["sales"]) == 3


def test_sample_day_view_matches_engine():
    sample = load_sample("returns")
    view = day_view(sample, "2026-11-05")
    assert view["date"] == "2026-11-05"
    assert view["net_margin_cents"] == -1600
    assert view["day"]["revenue_reversal_cents"] == 3600
    assert view["day"]["cost_reversal_cents"] == 2000


def test_missing_sample_raises():
    with pytest.raises(ReturnsInputError):
        load_sample("nope")


def test_prepare_merges_grouped_documents():
    options = prepare({"sales": [SALE], "refunds": [refund()], "acceptances": [acceptance()]})
    kinds = sorted(document_kind(raw) for raw in options["documents"])
    assert kinds == ["acceptance", "refund"]


def test_prepare_rejects_non_list_documents():
    with pytest.raises(ReturnsInputError):
        prepare({"sales": [SALE], "documents": {"not": "a list"}})


def test_summarize_adds_profile():
    result = summarize({"sales": [SALE]})
    assert result["profile"] == "returns"


def test_engine_can_be_used_directly():
    result = build_ledger(sales=[SALE], documents=[refund()])
    assert result["sales"][0]["revenue_reversal_cents"] == 3600


def test_request_key_is_stable_across_alias_forms():
    first = {"sales": [{"sale_id": "S1", "line_qty": "10", "net_revenue_cents": "9000"}]}
    second = {"sales": [{"sku": " S1 ", "qty": "１０", "net_revenue": "9,000"}]}
    assert request_key(first) == request_key(second)


def test_request_key_distinguishes_document_content():
    base = {"sales": [SALE], "documents": [refund()]}
    changed = {"sales": [SALE], "documents": [refund(amount=3601)]}
    assert request_key(base) != request_key(changed)


def test_request_key_ignores_whitespace_in_documents():
    first = {"sales": [SALE], "documents": [refund()]}
    second = {"sales": [SALE], "documents": [dict(refund(), credential_id=" RF1 ")]}
    assert request_key(first) == request_key(second)


def test_legacy_promotion_profile_becomes_sales():
    result = compute({"profile": "clean", "sale_date": "2026-11-01"})
    assert len(result["sales"]) == 3
    assert all(line["sale_date"] == "2026-11-01" for line in result["sales"])


def test_returns_profile_loads_bundled_sample():
    result = compute({"profile": "returns"})
    assert result["counts"]["documents"] == 6
    assert result["counts"]["replayed"] == 0


def test_day_view_empty_date_returns_zero():
    view = day_view({"profile": "returns"}, "2030-01-01")
    assert view["net_margin_cents"] == 0
    assert view["day"]["date"] == "2030-01-01"


def test_json_round_trip_is_serializable():
    result = compute({"profile": "returns"})
    assert json.loads(json.dumps(result, ensure_ascii=False))["totals"] == result["totals"]
