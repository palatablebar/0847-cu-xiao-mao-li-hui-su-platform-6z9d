from decimal import Decimal

from app.returns.engine import build_ledger, daily_margins, net_margin_for

SALE = {
    "sale_id": "S1",
    "sale_date": "2026-11-01",
    "line_qty": 10,
    "net_revenue_cents": 9000,
    "net_cost_cents": 5000,
}


def refund(credential="RF1", qty=4, day="2026-11-05", amount=3600, sale_id="S1"):
    return {"credential_id": credential, "kind": "refund", "sale_id": sale_id,
            "qty": qty, "received_date": day, "amount_cents": amount}


def acceptance(credential="AC1", qty=4, day="2026-11-03", sale_id="S1", **extra):
    row = {"credential_id": credential, "kind": "acceptance", "sale_id": sale_id,
           "qty": qty, "accepted_date": day}
    row.update(extra)
    return row


def ledger(documents, sales=None, date=None):
    return build_ledger(sales=sales or [SALE], documents=documents, date=date)


def day(result, date):
    return next(item for item in result["days"] if item["date"] == date)


def test_refund_day_reverses_revenue_but_not_cost():
    result = ledger([refund(day="2026-11-05"), acceptance(day="2026-11-07")])
    row = day(result, "2026-11-05")
    assert row["revenue_reversal_cents"] == 3600
    assert row["cost_reversal_cents"] == 0
    assert row["net_margin_cents"] == -3600
    line = result["sales"][0]
    assert line["cost_reversal_cents"] == 2000
    assert line["unmatched_refund_qty"] == 0


def test_cost_reverses_on_acceptance_day_when_refund_first():
    result = ledger([refund(day="2026-11-02"), acceptance(day="2026-11-04")])
    assert day(result, "2026-11-02")["cost_reversal_cents"] == 0
    assert day(result, "2026-11-04")["cost_reversal_cents"] == 2000
    assert day(result, "2026-11-04")["adjustment_cents"] == 2000


def test_acceptance_before_refund_waits_for_refund():
    result = ledger([acceptance(day="2026-11-01"), refund(day="2026-11-06")])
    assert day(result, "2026-11-01")["cost_reversal_cents"] == 0
    row = day(result, "2026-11-06")
    assert row["revenue_reversal_cents"] == 3600
    assert row["cost_reversal_cents"] == 2000


def test_only_both_refunded_and_accepted_quantity_reverses_cost():
    result = ledger([refund(qty=6), acceptance(qty=2)])
    line = result["sales"][0]
    assert line["refunded_qty"] == 6
    assert line["accepted_qty"] == 2
    assert line["matched_qty"] == 2
    assert line["unmatched_refund_qty"] == 4
    assert line["cost_reversal_cents"] == 1000


def test_excess_return_quantity_is_not_booked():
    documents = [refund("RF1", qty=8, day="2026-11-02", amount=7200),
                 refund("RF2", qty=5, day="2026-11-03", amount=4500),
                 acceptance("AC1", qty=20, day="2026-11-04")]
    result = ledger(documents)
    line = result["sales"][0]
    assert line["refunded_qty"] == 10
    assert line["revenue_reversal_cents"] <= 9000
    assert line["cost_reversal_cents"] <= 5000
    assert result["counts"]["excess_qty"] == 3
    assert any(issue["code"] == "EXCESS_RETURN" for issue in result["issues"])


def test_voided_acceptance_restores_cost_but_refund_stays():
    documents = [refund(day="2026-11-02"),
                 acceptance(day="2026-11-03"),
                 {"credential_id": "V1", "kind": "void", "target_doc_id": "AC1",
                  "voided_date": "2026-11-05"}]
    result = ledger(documents)
    assert day(result, "2026-11-03")["cost_reversal_cents"] == 2000
    assert day(result, "2026-11-05")["cost_reversal_cents"] == -2000
    line = result["sales"][0]
    assert line["cost_reversal_cents"] == 0
    assert line["revenue_reversal_cents"] == 3600
    assert line["accepted_qty"] == 0


def test_void_via_status_field():
    documents = [refund(day="2026-11-02"),
                 acceptance(day="2026-11-03", status="voided", voided_date="2026-11-04")]
    result = ledger(documents)
    assert result["sales"][0]["cost_reversal_cents"] == 0
    assert day(result, "2026-11-04")["cost_reversal_cents"] == -2000


def test_replayed_credential_does_not_double_book():
    document = refund()
    once = ledger([document])
    twice = ledger([document, dict(document)])
    assert once["sales"][0]["revenue_reversal_cents"] == twice["sales"][0]["revenue_reversal_cents"]
    assert twice["counts"]["replayed"] == 1
    assert any(issue["code"] == "REPLAYED_DOC" for issue in twice["issues"])


def test_conflicting_credential_keeps_first_document():
    result = ledger([refund(amount=3600), refund(amount=9999)])
    assert result["sales"][0]["revenue_reversal_cents"] == 3600
    assert result["counts"]["conflicts"] == 1


def test_missing_amount_falls_back_to_proportional_revenue():
    document = refund()
    document.pop("amount_cents")
    result = ledger([document])
    assert result["sales"][0]["revenue_reversal_cents"] == 3600


def test_amount_in_yuan_is_converted_to_cents():
    document = refund()
    document.pop("amount_cents")
    document["amount"] = "36.00"
    result = ledger([document])
    assert result["sales"][0]["revenue_reversal_cents"] == 3600


def test_day_query_returns_net_margin_for_date():
    result = ledger([refund(), acceptance(day="2026-11-07")], date="2026-11-05")
    assert result["selected_date"] == "2026-11-05"
    assert result["selected_day"]["net_margin_cents"] == -3600
    assert net_margin_for("2026-11-07", [SALE], [refund(), acceptance(day="2026-11-07")]) == 2000


def test_daily_margins_include_sale_day_and_adjustments():
    days = daily_margins([SALE], [refund(), acceptance(day="2026-11-07")])
    by_date = {row["date"]: row for row in days}
    assert by_date["2026-11-01"]["net_margin_cents"] == 4000
    assert by_date["2026-11-05"]["net_margin_cents"] == -3600
    assert by_date["2026-11-07"]["net_margin_cents"] == 2000


def test_totals_reconcile_with_lines():
    result = ledger([refund(), acceptance(day="2026-11-07")])
    totals = result["totals"]
    assert totals["net_margin_cents"] == sum(row["net_margin_cents"] for row in result["days"])
    assert totals["revenue_reversal_cents"] == result["sales"][0]["revenue_reversal_cents"]
    assert result["all_checks"] is True


def test_partial_acceptance_then_void_only_restores_matched_cost():
    documents = [refund(qty=4, day="2026-11-02"),
                 acceptance("AC1", qty=2, day="2026-11-03"),
                 acceptance("AC2", qty=2, day="2026-11-04"),
                 {"credential_id": "V1", "kind": "void", "target_doc_id": "AC2",
                  "voided_date": "2026-11-05"}]
    result = ledger(documents)
    line = result["sales"][0]
    assert line["matched_qty"] == 2
    assert line["cost_reversal_cents"] == 1000


def test_unknown_sale_document_is_isolated():
    result = ledger([refund(sale_id="UNKNOWN")])
    assert result["counts"]["isolated"] == 1
    assert any(issue["code"] == "UNKNOWN_SALE" for issue in result["issues"])
    assert result["sales"][0]["revenue_reversal_cents"] == 0


def test_sales_from_promotion_fields():
    sales = [{"sale_id": "P1", "sale_date": "2026-11-01", "line_qty": 10,
              "sell_price_cents": 900, "coupon_cents": 500,
              "unit_cost_cents": 600, "supplier_fund_cents": 1000}]
    result = ledger([refund(sale_id="P1", qty=4, amount=3600)], sales=sales)
    line = result["sales"][0]
    assert line["net_revenue_cents"] == 8500
    assert line["net_cost_cents"] == 5000
    assert line["revenue_reversal_cents"] == 3600


def test_documents_can_be_split_into_groups():
    result = build_ledger(sales=[SALE], refunds=[refund()], acceptances=[acceptance(day="2026-11-07")])
    assert result["sales"][0]["matched_qty"] == 4
    assert result["counts"]["refunds"] == 1
    assert result["counts"]["acceptances"] == 1


def test_money_helper_uses_decimal():
    from app.returns.engine import money
    assert money(1234) == Decimal("12.34")


def test_cost_reversal_rounds_without_losing_cents():
    sale = {"sale_id": "S1", "sale_date": "2026-11-01", "line_qty": 3,
            "net_revenue_cents": 3000, "net_cost_cents": 5000}
    documents = [refund("RF1", qty=1, day="2026-11-02", amount=1000),
                 refund("RF2", qty=1, day="2026-11-03", amount=1000),
                 acceptance("AC1", qty=3, day="2026-11-04")]
    result = ledger(documents, sales=[sale])
    assert result["sales"][0]["cost_reversal_cents"] == 3333
    assert sum(row["cost_reversal_cents"] for row in result["days"]) == 3333


def test_refund_amount_over_revenue_is_capped():
    documents = [refund(qty=10, amount=99999), acceptance(qty=10, day="2026-11-02")]
    result = ledger(documents)
    line = result["sales"][0]
    assert line["revenue_reversal_cents"] == 9000
    assert result["counts"]["excess_revenue"] == 99999 - 9000
    assert any(issue["code"] == "REVENUE_EXCESS" for issue in result["issues"])


def test_document_counts_are_conserved():
    documents = [
        refund("RF1"),
        refund("RF1"),
        refund("RF2", amount=1),
        refund("RF2", amount=2),
        {"kind": "refund", "sale_id": "S1", "qty": 1, "received_date": "2026-11-02"},
        {"credential_id": "X1", "kind": "refund", "sale_id": "MISSING", "qty": 1,
         "received_date": "2026-11-02", "amount_cents": 1},
        {"credential_id": "X2", "sale_id": "S1", "qty": 1, "date": "2026-11-02"},
    ]
    result = ledger(documents)
    counts = result["counts"]
    assert counts["documents"] == (counts["refunds"] + counts["acceptances"] + counts["voids"]
                                   + counts["replayed"] + counts["isolated"])
    assert result["checks"][0]["ok"] is True


def test_sale_rows_lacking_amount_still_reconcile():
    sales = [{"sale_id": "S1", "sale_date": "2026-11-01", "line_qty": 2,
              "sell_price_cents": 1000, "coupon_cents": 0, "unit_cost_cents": 600,
              "supplier_fund_cents": 0}]
    result = ledger([refund(qty=2, amount=2000), acceptance(qty=2, day="2026-11-02")],
                    sales=sales)
    assert result["sales"][0]["margin_cents"] == 800
    assert result["sales"][0]["net_margin_after_cents"] == 800 - 2000 + 1200
    assert result["all_checks"] is True
