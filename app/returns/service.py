"""退货跨日记账的服务层：输入归一化、幂等批次与 HTTP 便捷封装。"""

from __future__ import annotations

import hashlib
import json

from app.catalog import SPEC
from app.config import SAMPLES
from app.returns.engine import (SCHEMA, build_ledger, credential_of, document_kind,
                                document_signature)

RETURNS_PROFILE = "returns"

RUN_PREFIX = "returns:"


class ReturnsInputError(ValueError):
    """入参无法解析为退货账。"""


def load_sample(profile):
    """读取 sample_data/{profile}.json 的销售行与凭据。"""
    path = SAMPLES / f"{profile}.json"
    if not path.is_file():
        raise ReturnsInputError(f"样例不存在: {profile}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReturnsInputError(f"样例无法读取: {exc}")
    if not isinstance(data, dict):
        raise ReturnsInputError("样例必须是对象")
    return data


def _sales_from_payload(payload):
    sales = payload.get("sales")
    if sales is not None:
        if not isinstance(sales, list):
            raise ReturnsInputError("sales 必须是数组")
        return sales
    profile = payload.get("profile")
    if profile == RETURNS_PROFILE:
        return load_sample(profile).get("sales", [])
    if profile:
        from app.service import pipeline
        data = pipeline(profile, upto="毛利判定")
        rows = []
        for item in data.get("rows", []):
            row = {
                "sale_id": item.get("label") or item.get(SPEC["key"], ""),
                "line_qty": item.get("line_qty"),
                "net_revenue_cents": item.get("net_revenue"),
                "net_cost_cents": item.get("net_cost"),
            }
            if payload.get("sale_date"):
                row["sale_date"] = payload["sale_date"]
            rows.append(row)
        return rows
    return []


def _documents_from_payload(payload):
    documents = payload.get("documents")
    if documents is None:
        if payload.get("profile") == RETURNS_PROFILE:
            documents = load_sample(RETURNS_PROFILE).get("documents", [])
        else:
            documents = []
    if not isinstance(documents, list):
        raise ReturnsInputError("documents 必须是数组")
    merged = list(documents)
    for key, default_kind in (("refunds", "refund"), ("acceptances", "acceptance"),
                              ("voids", "void")):
        group = payload.get(key)
        if group is None:
            continue
        if not isinstance(group, list):
            raise ReturnsInputError(f"{key} 必须是数组")
        for raw in group:
            if isinstance(raw, dict):
                raw = dict(raw)
                # 分组入参的类型由分组决定，避免金额等字段误判。
                raw.setdefault("kind", default_kind)
            merged.append(raw)
    return merged


def prepare(payload, date=None):
    """把请求体转成引擎参数。"""
    if not isinstance(payload, dict):
        raise ReturnsInputError("请求体必须是对象")
    sales = _sales_from_payload(payload)
    documents = _documents_from_payload(payload)
    options = {
        "sales": sales,
        "documents": documents,
        "date": date or payload.get("date") or payload.get("day") or payload.get("on"),
        "default_date": payload.get("default_date"),
    }
    return options


def compute(payload, date=None):
    """执行退货记账，返回引擎结果。"""
    return build_ledger(**prepare(payload, date))


def ledger(payload, date=None):
    return compute(payload, date)


def request_key(payload):
    """同一组销售行与凭据（顺序无关）得到同一键，用于幂等重跑。"""
    options = prepare(payload)
    sales = sorted(
        json.dumps(_canonical_sale(item), ensure_ascii=False, sort_keys=True)
        if isinstance(item, dict) else str(item)
        for item in options["sales"])
    documents = []
    for raw in options["documents"]:
        if isinstance(raw, dict):
            documents.append(document_signature(raw, document_kind(raw)))
        else:
            documents.append(json.dumps(raw, ensure_ascii=False, sort_keys=True))
    documents.sort()
    # 选中日期影响批次中的"当日"快照，因此纳入幂等键。
    seed = json.dumps({"sales": sales, "documents": documents,
                       "date": options.get("date") or ""},
                      ensure_ascii=False, sort_keys=True)
    return RUN_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _canonical_sale(raw):
    """凭据键只覆盖业务字段，并做规范化，忽略空白、别名写法与字段顺序。"""
    from app.intake.normalize import canonical
    from app.returns import engine

    if not isinstance(raw, dict):
        return {"raw": canonical(raw)}

    def text(names):
        return engine._text(raw, names)

    if engine._has(raw, ("net_revenue_cents", "net_revenue")):
        revenue = text(("net_revenue_cents", "net_revenue"))
    else:
        revenue = [text(("sell_price_cents", "sell_price")), text(("coupon_cents", "coupon"))]
    if engine._has(raw, ("net_cost_cents", "net_cost")):
        cost = text(("net_cost_cents", "net_cost"))
    else:
        cost = [text(("unit_cost_cents", "unit_cost")), text(("supplier_fund_cents", "supplier_fund"))]
    return {
        "sale_id": text(engine.SALE_KEYS),
        "line_qty": text(("line_qty", "qty", "quantity")),
        "sale_date": text(("sale_date", "sold_date", "order_date")),
        "revenue": revenue,
        "cost": cost,
    }


def summarize(payload, date=None):
    result = compute(payload, date)
    result["profile"] = payload.get("profile") or RETURNS_PROFILE
    return result


def day_view(payload, date):
    result = compute(payload, date)
    selected = result.get("selected_day")
    return {
        "schema": SCHEMA,
        "date": result.get("selected_date"),
        "day": selected,
        "net_margin_cents": selected["net_margin_cents"] if selected else 0,
        "days": result.get("days", []),
        "sales": result.get("sales", []),
        "counts": result.get("counts", {}),
        "totals": result.get("totals", {}),
        "issues": result.get("issues", []),
        "checks": result.get("checks", []),
    }


def credentials_of(payload):
    return [credential_of(raw) for raw in _documents_from_payload(payload)
            if isinstance(raw, dict) and credential_of(raw)]
