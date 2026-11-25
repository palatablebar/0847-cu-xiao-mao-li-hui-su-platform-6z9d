from flask import Blueprint, abort, jsonify, request, send_file

from app.catalog import SPEC
from app.config import SAMPLES
from app.errors import DomainIssue
from app.evidence.render import render
from app.returns import service as returns
from app.review.service import approve, create_run, serialize
from app.service import pipeline


api = Blueprint("api", __name__)
STAGE_INDEX = {"intake": 1, "prepare": 2, "calculate": 3, "decision": 4, "reconcile": 5}


def store():
    from flask import current_app
    return current_app.extensions["runs"]


def returns_store():
    from flask import current_app
    return current_app.extensions["returns"]


@api.get("/api/catalog")
def catalog():
    return jsonify(code=SPEC["code"], name=SPEC["name"], actor=SPEC["actor"], rule=SPEC["rule"],
                   rule_short=SPEC["rule_short"],
                   stages=[{"id": item, "name": name} for item, name in zip(
                       ["intake", "prepare", "calculate", "decision", "reconcile", "review", "evidence"],
                       SPEC["stages"])],
                   chart=SPEC["chart"], result_columns=SPEC["result_columns"],
                   columns=SPEC["columns"], samples=["dirty", "clean", "returns"],
                   returns={"date": SPEC.get("returns_date"), "stages": [
                       {"id": "returns", "name": "退货跨日记账"}]})


@api.post("/api/stages/<stage_id>")
def stage(stage_id):
    if stage_id not in STAGE_INDEX:
        raise DomainIssue("STAGE_UNKNOWN", 404)
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    upto = SPEC["stages"][STAGE_INDEX[stage_id] - 1]
    return jsonify(pipeline(profile, upto=upto))


@api.post("/api/runs")
def create():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "dirty")
    payload = pipeline(profile)
    return jsonify(create_run(store(), payload))


@api.get("/api/runs/<run_id>")
def get_run(run_id):
    run = serialize(store().get(run_id))
    if not run:
        raise DomainIssue("RUN_NOT_FOUND", 404)
    return jsonify(run)


@api.post("/api/runs/<run_id>/review")
def review(run_id):
    data = request.get_json(silent=True) or {}
    if "decision" not in data or "expected_revision" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(approve(store(), run_id, data["decision"], data.get("comment", ""), int(data["expected_revision"])))


@api.get("/api/runs/<run_id>/evidence")
def evidence(run_id):
    run = serialize(store().get(run_id))
    return jsonify(render(run))


@api.get("/api/examples/<profile>")
def example(profile):
    if profile == "returns":
        return send_file(SAMPLES / "returns.json", as_attachment=True,
                         download_name="returns.json", mimetype="application/json")
    if profile not in {"clean", "dirty"}:
        abort(404)
    return send_file(SAMPLES / f"{profile}.csv", as_attachment=True)


# ---------------------------------------------------------------------------
# 退货跨日记账
# ---------------------------------------------------------------------------

@api.post("/api/returns/ledger")
def returns_ledger():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    try:
        return jsonify(returns.summarize(_returns_source(data)))
    except returns.ReturnsInputError as exc:
        raise DomainIssue("BAD_REQUEST", detail=str(exc))


@api.route("/api/returns/day", methods=["GET", "POST"])
def returns_day():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    date = request.args.get("date") or data.get("date")
    if not date:
        raise DomainIssue("BAD_REQUEST", detail="缺少 date")
    if not any(key in data for key in ("sales", "documents", "refunds", "acceptances", "voids")):
        snapshot = returns_store().snapshot()
        if data.get("stored") is not False and (snapshot["sales"] or snapshot["documents"]):
            data = dict(snapshot, date=date)
        else:
            data = dict(data, profile=data.get("profile") or returns.RETURNS_PROFILE, date=date)
    try:
        return jsonify(returns.day_view(_returns_source(data), date))
    except returns.ReturnsInputError as exc:
        raise DomainIssue("BAD_REQUEST", detail=str(exc))


@api.post("/api/returns/runs")
def returns_create_run():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    try:
        data = _returns_source(data)
        payload = returns.compute(data)
        key = returns.request_key(data)
    except returns.ReturnsInputError as exc:
        raise DomainIssue("BAD_REQUEST", detail=str(exc))
    body = {
        "project": SPEC["code"] + " " + SPEC["name"],
        "rule": "退货跨日: 收入按到账日冲减, 成本按已退款且已验收数量冲回",
        "profile": data.get("profile") or returns.RETURNS_PROFILE,
        "source": {"profile": data.get("profile") or returns.RETURNS_PROFILE,
                   "filename": "returns", "sha256": key},
        "summary": _returns_summary(payload),
        "returns": payload,
        "counts": payload["counts"],
        "ledger": {
            "counts": payload["counts"],
            "total_net_margin_cents": payload["totals"]["net_margin_cents"],
            "days": payload["days"],
            "checks": payload["checks"],
            "all_checks": payload["all_checks"],
            "balanced": payload["balanced"],
        },
        "chart": [{"label": item["date"],
                   "value": item["net_margin_cents"] / 100,
                   "limit": item["base_margin_cents"] / 100}
                  for item in payload["days"][-12:]],
        "run_key": key,
    }
    return jsonify(create_run(store(), body))


@api.post("/api/returns/ingest")
def returns_ingest():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    sales = data.get("sales")
    documents = data.get("documents")
    if sales is None and documents is None:
        sample = returns.load_sample(data.get("profile") or returns.RETURNS_PROFILE)
        sales, documents = sample.get("sales", []), sample.get("documents", [])
    if sales is not None and not isinstance(sales, list):
        raise DomainIssue("BAD_REQUEST", detail="sales 必须是数组")
    if documents is not None and not isinstance(documents, list):
        raise DomainIssue("BAD_REQUEST", detail="documents 必须是数组")
    db = returns_store()
    result = {"sales": db.ingest_sales(sales or []),
              "documents": db.ingest_documents(documents or [])}
    result["snapshot"] = _returns_ledger_of(db.snapshot(), data.get("date"))
    return jsonify(result)


@api.get("/api/returns/state")
def returns_state():
    db = returns_store()
    snapshot = db.snapshot()
    return jsonify({"sales": snapshot["sales"], "documents": snapshot["documents"],
                    "ledger": _returns_ledger_of(snapshot, request.args.get("date"))})


@api.delete("/api/returns/state")
def returns_reset():
    returns_store().clear()
    return jsonify(status="ok")


def _returns_ledger_of(snapshot, date=None):
    return returns.summarize({"sales": snapshot["sales"], "documents": snapshot["documents"],
                              "date": date})


def _returns_source(data):
    """stored=true 时使用本地持久化凭据集合；否则使用请求体或样例。"""
    if data.get("stored"):
        snapshot = returns_store().snapshot()
        merged = {"sales": snapshot["sales"], "documents": snapshot["documents"]}
        if data.get("date"):
            merged["date"] = data["date"]
        return merged
    return data


def _returns_summary(body):
    summary = {"PASS": 0, "REVIEW": 0, "REJECT": 0}
    for check in body["checks"]:
        if not check["ok"]:
            summary["REJECT"] += 1
    if summary["REJECT"] == 0 and body["issues"]:
        summary["REVIEW"] = 1
    if summary["REJECT"] == 0 and not body["issues"]:
        if body["sales"]:
            summary["PASS"] = len(body["sales"])
        else:
            summary["REVIEW"] = 1
    return summary
