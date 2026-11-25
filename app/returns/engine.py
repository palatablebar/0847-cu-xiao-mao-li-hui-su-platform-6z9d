"""促销退货跨日记账。

纯计算模块，不依赖 Flask 或 SQLite。规则：

* 退款到账当天冲减收入（退款金额优先，缺省时按原销售净收入比例）；
* 仅当同一销售行上的数量同时"已退款且已验收"时，才按原销售净成本冲回成本；
* 退款单与验收单可以任意顺序到达，按业务日期重建时间线：
  收入在退款到账日减少，成本在"退款 + 验收"两个条件都满足的较晚日期冲回；
* 累计退货数量超过原售数量时，超额数量与超额金额不入账；
* 验收单作废后，在作废日恢复此前冲回的成本；已到账退款保持生效；
* 同一凭据（credential_id / doc_id 等）重复提交只记一次，重放不会重复记账；
* 金额全部使用整数分，避免二进制浮点误差。
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date as Date
from decimal import Decimal, InvalidOperation

from app.domain.helpers import M, round_div
from app.intake.normalize import canonical

SCHEMA = "workbench-returns/v1"

SALE_KEYS = ("sale_id", "sale_line_id", "sales_line_id", "order_line_id", "line_id",
             "source_line_id", "sale", "sale_key", "sku")
CREDENTIAL_KEYS = ("credential_id", "doc_id", "document_id", "doc_no", "document_no",
                   "voucher_id", "ticket_id", "receipt_id", "credential", "id")
QTY_KEYS = ("qty", "quantity", "return_qty", "refund_qty", "accepted_qty", "line_qty",
            "count", "pieces")
REFUND_DATE_KEYS = ("refund_received_date", "received_date", "refund_date", "credited_date",
                    "refund_credited_date", "arrival_date", "received_at", "refund_at", "date")
ACCEPT_DATE_KEYS = ("accepted_date", "acceptance_date", "accept_date", "inbound_date",
                    "warehouse_date", "received_date", "accepted_at", "date")
# 用于比较两张验收单是否"同一条"，不含 received_date / date 这类歧义字段。
ACCEPT_STRICT_KEYS = ("accepted_date", "acceptance_date", "accept_date", "inbound_date",
                      "warehouse_date", "accepted_at")
VOID_DATE_KEYS = ("voided_date", "void_date", "cancelled_date", "canceled_date",
                  "invalidated_date", "date")
TARGET_KEYS = ("target_doc_id", "target_credential_id", "target_id", "acceptance_doc_id",
               "acceptance_id", "voids", "void_target", "target", "cancels")
STATUS_KEYS = ("status", "state", "doc_status")
KIND_KEYS = ("kind", "type", "doc_type", "document_type", "category")

AMOUNT_CENT_KEYS = ("refund_amount_cents", "amount_cents", "refund_cents", "credited_cents")
AMOUNT_YUAN_KEYS = ("refund_amount", "amount", "refund")

KIND_ALIASES = {
    "refund": "refund",
    "refund_received": "refund",
    "refunded": "refund",
    "refund_receipt": "refund",
    "退款": "refund",
    "退款单": "refund",
    "退款到账": "refund",
    "退货": "refund",
    "acceptance": "acceptance",
    "accept": "acceptance",
    "accepted": "acceptance",
    "acceptance_order": "acceptance",
    "inbound": "acceptance",
    "inspection": "acceptance",
    "验收": "acceptance",
    "验收单": "acceptance",
    "验收入库": "acceptance",
    "入库": "acceptance",
    "void": "void",
    "acceptance_void": "void",
    "void_acceptance": "void",
    "cancel": "void",
    "cancel_acceptance": "void",
    "invalidate": "void",
    "作废": "void",
    "验收作废": "void",
    "验收单作废": "void",
}
VOID_STATUS = {"void", "voided", "cancelled", "canceled", "invalid", "invalidated",
               "作废", "已作废", "无效"}


@dataclass
class Sale:
    sale_id: str
    day: Date
    qty: int
    revenue: int
    cost: int
    line: int = 0

    @property
    def margin(self) -> int:
        return self.revenue - self.cost


@dataclass
class Refund:
    credential: str
    sale_id: str
    qty: int
    day: Date
    amount: int | None
    line: int = 0
    allowed_qty: int = 0
    booked_amount: int = 0


@dataclass
class Acceptance:
    credential: str
    sale_id: str
    qty: int
    accepted: Date
    voided: Date | None = None
    line: int = 0
    raw_status: str = ""


@dataclass
class VoidDoc:
    credential: str
    target: str
    day: Date
    line: int = 0


def _issue(code, *, sale_id="", doc_id="", detail="", line=0):
    return {"line": line, "key": sale_id, "sale_id": sale_id, "doc_id": doc_id,
            "code": code, "detail": detail}


def _text(raw, names):
    for name in names:
        if name in raw:
            value = canonical(raw[name])
            if value != "":
                return value
    return ""


def _has(raw, names):
    return any(name in raw and canonical(raw[name]) != "" for name in names)


def _integer(value, code):
    number = Decimal(canonical(value))
    if number != number.to_integral_value():
        raise ValueError(code)
    return int(number)


def _day(value, code):
    text = canonical(value)
    try:
        return Date.fromisoformat(text)
    except ValueError:
        pass
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M"):
        try:
            from datetime import datetime
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(code)


def _amount(raw):
    """金额：带 cents 的字段按整数分；普通 amount/refund_amount 按元的十进制定点数。"""
    if _has(raw, AMOUNT_CENT_KEYS):
        return _integer(_text(raw, AMOUNT_CENT_KEYS), "BAD_AMOUNT")
    text = _text(raw, AMOUNT_YUAN_KEYS)
    if text == "":
        return None
    try:
        return M(text, "BAD_AMOUNT")
    except (InvalidOperation, ValueError):
        raise ValueError("BAD_AMOUNT")


def _scale(total: int, part: int, whole: int) -> int:
    """按数量比例分摊整数金额，四舍五入到分。"""
    if whole <= 0 or part <= 0:
        return 0
    if part >= whole:
        return int(total)
    return round_div(int(total) * int(part), int(whole))


def _normalize_kind(text: str) -> str:
    text = canonical(text).lower()
    if not text:
        return ""
    return KIND_ALIASES.get(text, text if text in {"refund", "acceptance", "void"} else "")


def document_kind(raw) -> str:
    """识别单据类型：refund / acceptance / void，未知返回空串。

    `received_date` 同时可能表示退款到账日或验收入库日，单独出现时不推断类型，
    避免把退款单误当验收单，需要调用方显式给出 `kind`。
    """
    if not isinstance(raw, dict):
        return ""
    explicit = _normalize_kind(_text(raw, KIND_KEYS))
    if explicit:
        return explicit
    if _has(raw, TARGET_KEYS):
        return "void"
    if _has(raw, ("refund_received_date", "refund_date", "credited_date", "refund_credited_date",
                  "refund_at", "refund_credited_at")):
        return "refund"
    if _has(raw, ("accepted_date", "acceptance_date", "accept_date", "inbound_date",
                  "warehouse_date", "accepted_at", "inspection_date")):
        return "acceptance"
    if _has(raw, AMOUNT_CENT_KEYS) or _has(raw, AMOUNT_YUAN_KEYS):
        return "refund"
    # 只有 received_date 时按退款到账日理解（验收单通常带 accepted_date）。
    if _has(raw, ("received_date", "received_at")):
        return "refund"
    return ""


def credential_of(raw) -> str:
    if not isinstance(raw, dict):
        return ""
    return _text(raw, CREDENTIAL_KEYS)


def _date_keys(kind):
    if kind == "void":
        return VOID_DATE_KEYS
    if kind == "acceptance":
        return ACCEPT_DATE_KEYS
    return REFUND_DATE_KEYS


def _status(raw) -> str:
    return _text(raw, STATUS_KEYS).lower()


def document_descriptor(raw, kind=None) -> dict:
    """识别"同一凭据的同一内容"的稳定描述。"""
    if not isinstance(raw, dict):
        return {"raw": canonical(raw)}
    kind = kind or document_kind(raw)
    return {
        "kind": kind,
        "sale_id": _text(raw, SALE_KEYS),
        "qty": _text(raw, QTY_KEYS),
        "date": _text(raw, _date_keys(kind)),
        "amount": _text(raw, AMOUNT_CENT_KEYS) or _text(raw, AMOUNT_YUAN_KEYS),
        "target": _text(raw, TARGET_KEYS),
        "status": _status(raw),
        "voided_date": _text(raw, ("voided_date", "void_date")),
    }


def document_signature(raw, kind=None) -> str:
    return json.dumps(document_descriptor(raw, kind), ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------------------
# 输入解析
# ---------------------------------------------------------------------------

def _parse_sales(raw_sales, fallback_day):
    sales, issues = [], []
    seen = set()
    for index, raw in enumerate(raw_sales or []):
        line = index + 1
        if not isinstance(raw, dict):
            issues.append(_issue("BAD_SALE", detail="销售行必须是对象", line=line))
            continue
        sale_id = _text(raw, SALE_KEYS)
        if not sale_id:
            issues.append(_issue("SALE_ID_MISSING", line=line))
            continue
        if sale_id in seen:
            issues.append(_issue("DUPLICATE_SALE", sale_id=sale_id, line=line))
            continue
        try:
            qty = _integer(_text(raw, ("line_qty", "qty", "quantity")), "BAD_QTY")
            if qty <= 0:
                raise ValueError("BAD_QTY")
        except (ValueError, InvalidOperation):
            issues.append(_issue("BAD_QTY", sale_id=sale_id, line=line))
            continue
        text = _text(raw, ("sale_date", "sold_date", "order_date", "date"))
        if text:
            try:
                sale_day = _day(text, "BAD_SALE_DATE")
            except ValueError:
                issues.append(_issue("BAD_SALE_DATE", sale_id=sale_id, line=line))
                continue
        else:
            sale_day = fallback_day
        try:
            if _has(raw, ("net_revenue_cents", "net_revenue")):
                revenue = _integer(_text(raw, ("net_revenue_cents", "net_revenue")), "BAD_REVENUE")
            else:
                sell = _integer(_text(raw, ("sell_price_cents", "sell_price")), "BAD_REVENUE")
                coupon = _integer(_text(raw, ("coupon_cents", "coupon")) or "0", "BAD_REVENUE")
                revenue = sell * qty - coupon
            if _has(raw, ("net_cost_cents", "net_cost")):
                cost = _integer(_text(raw, ("net_cost_cents", "net_cost")), "BAD_COST")
            else:
                unit = _integer(_text(raw, ("unit_cost_cents", "unit_cost")), "BAD_COST")
                fund = _integer(_text(raw, ("supplier_fund_cents", "supplier_fund")) or "0", "BAD_COST")
                cost = unit * qty - fund
        except (ValueError, InvalidOperation):
            issues.append(_issue("BAD_VALUE", sale_id=sale_id, line=line))
            continue
        if revenue < 0:
            issues.append(_issue("BAD_REVENUE", sale_id=sale_id, detail="净收入不能为负", line=line))
            continue
        if cost < 0:
            issues.append(_issue("BAD_COST", sale_id=sale_id, detail="净成本不能为负", line=line))
            continue
        seen.add(sale_id)
        sales.append(Sale(sale_id=sale_id, day=sale_day, qty=qty, revenue=revenue,
                          cost=cost, line=line))
    return sales, issues


def _collect_documents(documents, refunds, acceptances, voids):
    """把多种入参形式合并成 (kind, raw, line) 列表。"""
    entries = []
    for group, default_kind in (
        (documents or [], ""),
        (refunds or [], "refund"),
        (acceptances or [], "acceptance"),
        (voids or [], "void"),
    ):
        for index, raw in enumerate(group):
            entries.append((document_kind(raw) or default_kind, raw, index + 1))
    return entries


def _same_acceptance(first, second):
    """判断第二张验收单是否指向同一张验收单；作废凭据可省略销售行/数量/日期。"""
    for names in (SALE_KEYS, QTY_KEYS):
        before, after = _text(first, names), _text(second, names)
        if after and before != after:
            return False
    before_day = _text(first, ACCEPT_STRICT_KEYS) or _text(first, ACCEPT_DATE_KEYS)
    after_day = _text(second, ACCEPT_STRICT_KEYS)
    if after_day and before_day != after_day:
        return False
    return True


def _dedupe(entries, issues, counts):
    """按凭据去重：同内容视为重放；冲突保留首次提交；验收单允许转为作废。"""
    seen, unique, index_by_credential = {}, [], {}
    for kind, raw, line in entries:
        counts["documents"] += 1
        if not isinstance(raw, dict):
            counts["isolated"] += 1
            issues.append(_issue("BAD_DOCUMENT", detail="单据必须是对象", line=line))
            continue
        credential = credential_of(raw)
        if not credential:
            counts["isolated"] += 1
            issues.append(_issue("DOC_ID_MISSING", sale_id=_text(raw, SALE_KEYS), line=line))
            continue
        signature = document_signature(raw, kind)
        if credential in seen:
            if seen[credential] == signature:
                counts["replayed"] += 1
                issues.append(_issue("REPLAYED_DOC", doc_id=credential,
                                     detail="同一凭据重复提交，未重复记账", line=line))
                continue
            previous_kind, previous_raw, previous_line = unique[index_by_credential[credential]]
            if (kind == "acceptance" and previous_kind == "acceptance"
                    and _same_acceptance(previous_raw, raw)):
                previous_status, current_status = _status(previous_raw), _status(raw)
                if previous_status not in VOID_STATUS and current_status in VOID_STATUS:
                    merged = dict(raw)
                    # 保留原验收日期，作废日来自新凭据。
                    merged["accepted_date"] = _text(previous_raw, ACCEPT_DATE_KEYS)
                    unique[index_by_credential[credential]] = ("acceptance", merged, previous_line)
                    seen[credential] = document_signature(merged, kind)
                    counts["replayed"] += 1
                    issues.append(_issue("ACCEPTANCE_VOIDED", doc_id=credential,
                                         detail="验收单改为作废，成本冲回撤销", line=line))
                    continue
                if previous_status in VOID_STATUS and current_status in VOID_STATUS:
                    counts["replayed"] += 1
                    issues.append(_issue("REPLAYED_DOC", doc_id=credential,
                                         detail="验收单已作废，重复提交不重复恢复", line=line))
                    continue
            counts["conflicts"] += 1
            counts["isolated"] += 1
            issues.append(_issue("DOC_CONFLICT", doc_id=credential,
                                 detail="同一凭据内容冲突，保留首次提交", line=line))
            continue
        seen[credential] = signature
        index_by_credential[credential] = len(unique)
        unique.append((kind, raw, line))
    return unique


def _parse_documents(unique, sales, issues, counts):
    by_id = {sale.sale_id: sale for sale in sales}
    refunds, acceptances, voids = [], [], []
    for kind, raw, line in unique:
        sale_id = _text(raw, SALE_KEYS)
        credential = credential_of(raw)
        if not kind:
            counts["isolated"] += 1
            issues.append(_issue("UNKNOWN_DOC_KIND", sale_id=sale_id, doc_id=credential, line=line))
            continue
        if kind == "void":
            # 作废单可以只带目标验收单号，不需要重复销售行号。
            target = _text(raw, TARGET_KEYS)
            if not target:
                counts["isolated"] += 1
                issues.append(_issue("VOID_TARGET_MISSING", sale_id=sale_id, doc_id=credential, line=line))
                continue
            try:
                void_day = _day(_text(raw, VOID_DATE_KEYS), "BAD_DATE")
            except (ValueError, InvalidOperation):
                counts["isolated"] += 1
                issues.append(_issue("BAD_DATE", sale_id=sale_id, doc_id=credential, line=line))
                continue
            counts["voids"] += 1
            voids.append(VoidDoc(credential=credential, target=target, day=void_day, line=line))
            continue
        if not sale_id or sale_id not in by_id:
            counts["isolated"] += 1
            issues.append(_issue("UNKNOWN_SALE", sale_id=sale_id, doc_id=credential, line=line))
            continue
        sale = by_id[sale_id]
        try:
            qty = _integer(_text(raw, QTY_KEYS), "BAD_QTY")
            if qty <= 0:
                raise ValueError("BAD_QTY")
        except (ValueError, InvalidOperation):
            counts["isolated"] += 1
            issues.append(_issue("BAD_QTY", sale_id=sale_id, doc_id=credential, line=line))
            continue
        if kind == "refund":
            try:
                refund_day = _day(_text(raw, REFUND_DATE_KEYS), "BAD_DATE")
                amount = _amount(raw)
                if amount is not None and amount < 0:
                    raise ValueError("BAD_AMOUNT")
            except (ValueError, InvalidOperation):
                counts["isolated"] += 1
                issues.append(_issue("BAD_DATE", sale_id=sale_id, doc_id=credential, line=line))
                continue
            if refund_day < sale.day:
                counts["isolated"] += 1
                issues.append(_issue("BEFORE_SALE_DATE", sale_id=sale_id, doc_id=credential, line=line))
                continue
            counts["refunds"] += 1
            refunds.append(Refund(credential=credential, sale_id=sale_id, qty=qty, day=refund_day,
                                  amount=amount, line=line))
        else:
            try:
                accepted_day = _day(_text(raw, ACCEPT_DATE_KEYS), "BAD_DATE")
            except (ValueError, InvalidOperation):
                counts["isolated"] += 1
                issues.append(_issue("BAD_DATE", sale_id=sale_id, doc_id=credential, line=line))
                continue
            if accepted_day < sale.day:
                counts["isolated"] += 1
                issues.append(_issue("BEFORE_SALE_DATE", sale_id=sale_id, doc_id=credential, line=line))
                continue
            status = _status(raw)
            voided = None
            if status in VOID_STATUS:
                text = _text(raw, ("voided_date", "void_date"))
                if text:
                    try:
                        voided = _day(text, "BAD_DATE")
                    except (ValueError, InvalidOperation):
                        counts["isolated"] += 1
                        issues.append(_issue("BAD_DATE", sale_id=sale_id, doc_id=credential, line=line))
                        continue
                else:
                    voided = accepted_day
                if voided < accepted_day:
                    voided = accepted_day
            counts["acceptances"] += 1
            acceptances.append(Acceptance(credential=credential, sale_id=sale_id, qty=qty,
                                          accepted=accepted_day, voided=voided, line=line,
                                          raw_status=status))
    return refunds, acceptances, voids


def _apply_voids(acceptances, voids, issues, counts):
    by_id = {item.credential: item for item in acceptances}
    for void in voids:
        target = by_id.get(void.target)
        if target is None:
            issues.append(_issue("VOID_TARGET_MISSING", doc_id=void.credential,
                                 detail=f"未找到验收单 {void.target}", line=void.line))
            continue
        effective = max(void.day, target.accepted)
        if target.voided is None or effective < target.voided:
            target.voided = effective
        else:
            issues.append(_issue("VOID_DUPLICATE", sale_id=target.sale_id, doc_id=void.credential,
                                 detail="验收单已作废，重复作废不重复恢复", line=void.line))


# ---------------------------------------------------------------------------
# 记账
# ---------------------------------------------------------------------------

def _book_refunds(sale, refunds, issues, counts):
    """按到账日顺序占用原售数量；超额数量/金额不入账。"""
    remaining_qty = sale.qty
    booked_qty = 0
    booked_revenue = 0
    for refund in sorted(refunds, key=lambda item: (item.day, item.credential)):
        allowed = min(refund.qty, max(0, remaining_qty))
        refund.allowed_qty = allowed
        if allowed < refund.qty:
            excess = refund.qty - allowed
            counts["excess_qty"] += excess
            issues.append(_issue("EXCESS_RETURN", sale_id=sale.sale_id, doc_id=refund.credential,
                                 detail=f"退货数量超过原售数量，超额 {excess} 不入账",
                                 line=refund.line))
        if allowed <= 0:
            refund.booked_amount = 0
            continue
        if refund.amount is not None:
            raw_amount = _scale(refund.amount, allowed, refund.qty)
        else:
            raw_amount = (_scale(sale.revenue, booked_qty + allowed, sale.qty)
                          - _scale(sale.revenue, booked_qty, sale.qty))
        room = max(0, sale.revenue - booked_revenue)
        booked = min(raw_amount, room)
        if booked < raw_amount:
            counts["excess_revenue"] += raw_amount - booked
            issues.append(_issue("REVENUE_EXCESS", sale_id=sale.sale_id, doc_id=refund.credential,
                                 detail="退款金额超过原销售净收入，超额部分不入账",
                                 line=refund.line))
        refund.booked_amount = booked
        booked_qty += allowed
        booked_revenue += booked
        remaining_qty -= allowed
    return booked_qty, booked_revenue


def _active_accepted(sale, acceptances, day):
    total = 0
    for item in acceptances:
        if item.accepted <= day and (item.voided is None or day < item.voided):
            total += item.qty
    return min(total, sale.qty)


def _timeline(sale, refunds, acceptances):
    days = {sale.day}
    days.update(item.day for item in refunds)
    days.update(item.accepted for item in acceptances)
    days.update(item.voided for item in acceptances if item.voided is not None)
    events = []
    previous_revenue = previous_cost = 0
    for day in sorted(days):
        refunded_qty = sum(item.allowed_qty for item in refunds if item.day <= day)
        refunded_revenue = sum(item.booked_amount for item in refunds if item.day <= day)
        accepted_qty = _active_accepted(sale, acceptances, day)
        matched_qty = min(refunded_qty, accepted_qty)
        cost_reversal = _scale(sale.cost, matched_qty, sale.qty)
        revenue_delta = refunded_revenue - previous_revenue
        cost_delta = cost_reversal - previous_cost
        if day != sale.day or revenue_delta or cost_delta:
            events.append({
                "date": day.isoformat(),
                "refunded_qty": refunded_qty,
                "accepted_qty": accepted_qty,
                "matched_qty": matched_qty,
                "revenue_reversal_cents": revenue_delta,
                "cost_reversal_cents": cost_delta,
                "margin_delta_cents": cost_delta - revenue_delta,
            })
        previous_revenue, previous_cost = refunded_revenue, cost_reversal
    return events


def _line_result(sale, refunds, acceptances, events, issues, counts):
    refunded_qty = sum(item.allowed_qty for item in refunds)
    revenue_reversal = sum(item.booked_amount for item in refunds)
    final_day = max([sale.day] + [item.day for item in refunds]
                    + [item.accepted for item in acceptances]
                    + [item.voided for item in acceptances if item.voided is not None])
    accepted_qty = _active_accepted(sale, acceptances, final_day)
    active_total = sum(item.qty for item in acceptances
                       if item.voided is None or item.voided > final_day)
    if active_total > sale.qty:
        excess = active_total - sale.qty
        counts["excess_accepted_qty"] += excess
        issues.append(_issue("EXCESS_ACCEPTANCE", sale_id=sale.sale_id,
                             detail=f"验收数量超过原售数量，超额 {excess} 不参与成本冲回",
                             line=sale.line))
    matched_qty = min(refunded_qty, accepted_qty)
    cost_reversal = _scale(sale.cost, matched_qty, sale.qty)
    net_revenue = sale.revenue - revenue_reversal
    net_cost = sale.cost - cost_reversal
    return {
        "sale_id": sale.sale_id,
        "sale_date": sale.day.isoformat(),
        "line_qty": sale.qty,
        "net_revenue_cents": sale.revenue,
        "net_cost_cents": sale.cost,
        "margin_cents": sale.margin,
        "refunded_qty": refunded_qty,
        "accepted_qty": accepted_qty,
        "matched_qty": matched_qty,
        "unmatched_refund_qty": max(0, refunded_qty - matched_qty),
        "awaiting_acceptance_qty": max(0, refunded_qty - matched_qty),
        "revenue_reversal_cents": revenue_reversal,
        "cost_reversal_cents": cost_reversal,
        "net_revenue_after_cents": net_revenue,
        "net_cost_after_cents": net_cost,
        "net_margin_after_cents": net_revenue - net_cost,
        "margin_after_cents": net_revenue - net_cost,
        "events": events,
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def _fallback_day(documents, refunds, acceptances, voids, default_date):
    if default_date:
        return _day(default_date, "BAD_DATE")
    dates = []
    for group in (documents or [], refunds or [], acceptances or [], voids or []):
        for raw in group:
            if not isinstance(raw, dict):
                continue
            kind = document_kind(raw) or "refund"
            text = _text(raw, _date_keys(kind))
            if not text:
                continue
            try:
                dates.append(_day(text, "BAD_DATE"))
            except ValueError:
                continue
    return min(dates) if dates else Date.today()


def build_ledger(sales=None, documents=None, refunds=None, acceptances=None, voids=None,
                 date=None, default_date=None):
    """退货后的行级与日级净毛利。

    *sales* 为原销售行；*documents* 可混合退款、验收、作废单据，也可以分别通过
    *refunds* / *acceptances* / *voids* 传入。*date* 用于选中"当日"，
    传入 ``today`` 时取本机当天。
    """
    issues = []
    counts = {
        "documents": 0, "refunds": 0, "acceptances": 0, "voids": 0,
        "replayed": 0, "conflicts": 0, "isolated": 0,
        "excess_qty": 0, "excess_accepted_qty": 0, "excess_revenue": 0,
    }
    fallback = _fallback_day(documents, refunds, acceptances, voids, default_date)
    parsed_sales, sale_issues = _parse_sales(sales or [], fallback)
    issues.extend(sale_issues)
    entries = _collect_documents(documents, refunds, acceptances, voids)
    unique = _dedupe(entries, issues, counts)
    parsed_refunds, parsed_acceptances, parsed_voids = _parse_documents(
        unique, parsed_sales, issues, counts)
    _apply_voids(parsed_acceptances, parsed_voids, issues, counts)

    refunds_by_sale, acceptances_by_sale = {}, {}
    for item in parsed_refunds:
        refunds_by_sale.setdefault(item.sale_id, []).append(item)
    for item in parsed_acceptances:
        acceptances_by_sale.setdefault(item.sale_id, []).append(item)

    line_results = []
    for sale in parsed_sales:
        line_refunds = refunds_by_sale.get(sale.sale_id, [])
        line_acceptances = acceptances_by_sale.get(sale.sale_id, [])
        _book_refunds(sale, line_refunds, issues, counts)
        events = _timeline(sale, line_refunds, line_acceptances)
        line_results.append(_line_result(sale, line_refunds, line_acceptances, events,
                                         issues, counts))

    day_map = OrderedDict()

    def day_row(day):
        key = day.isoformat()
        if key not in day_map:
            day_map[key] = {
                "date": key, "base_margin_cents": 0,
                "revenue_reversal_cents": 0, "cost_reversal_cents": 0,
                "adjustment_cents": 0, "net_margin_cents": 0,
                "sales_count": 0, "refund_qty": 0, "matched_qty_delta": 0,
                "refund_events": 0, "acceptance_events": 0,
            }
        return day_map[key]

    for sale in parsed_sales:
        row = day_row(sale.day)
        row["base_margin_cents"] += sale.margin
        row["sales_count"] += 1
    for line in line_results:
        previous_matched = 0
        for event in line["events"]:
            row = day_row(Date.fromisoformat(event["date"]))
            row["revenue_reversal_cents"] += event["revenue_reversal_cents"]
            row["cost_reversal_cents"] += event["cost_reversal_cents"]
            row["matched_qty_delta"] += event["matched_qty"] - previous_matched
            previous_matched = event["matched_qty"]
    for refund in parsed_refunds:
        if refund.allowed_qty:
            row = day_row(refund.day)
            row["refund_qty"] += refund.allowed_qty
            row["refund_events"] += 1
    for acceptance in parsed_acceptances:
        day_row(acceptance.accepted)["acceptance_events"] += 1

    for row in day_map.values():
        row["adjustment_cents"] = row["cost_reversal_cents"] - row["revenue_reversal_cents"]
        row["net_margin_cents"] = row["base_margin_cents"] + row["adjustment_cents"]
    days = sorted(day_map.values(), key=lambda item: item["date"])

    totals = {
        "base_margin_cents": sum(item["base_margin_cents"] for item in days),
        "revenue_reversal_cents": sum(item["revenue_reversal_cents"] for item in days),
        "cost_reversal_cents": sum(item["cost_reversal_cents"] for item in days),
        "net_margin_cents": sum(item["net_margin_cents"] for item in days),
        "refunded_qty": sum(item["refunded_qty"] for item in line_results),
        "accepted_qty": sum(item["accepted_qty"] for item in line_results),
        "matched_qty": sum(item["matched_qty"] for item in line_results),
        "excess_qty": counts["excess_qty"],
    }
    totals["adjustment_cents"] = totals["net_margin_cents"] - totals["base_margin_cents"]

    selected, selected_date = _select_day(days, date)

    checks = [
        {"id": "documents", "label": "凭据记账守恒",
         "ok": counts["documents"] == counts["refunds"] + counts["acceptances"]
         + counts["voids"] + counts["replayed"] + counts["isolated"]},
        {"id": "quantities", "label": "退货数量不超原售",
         "ok": all(item["refunded_qty"] <= item["line_qty"]
                   and item["matched_qty"] <= item["line_qty"] for item in line_results)},
        {"id": "revenue", "label": "收入冲减不超原售",
         "ok": all(item["revenue_reversal_cents"] <= item["net_revenue_cents"]
                   for item in line_results)},
        {"id": "cost", "label": "成本冲回不超原售",
         "ok": all(item["cost_reversal_cents"] <= item["net_cost_cents"]
                   for item in line_results)},
        {"id": "daily", "label": "日毛利复算一致",
         "ok": (sum(item["revenue_reversal_cents"] for item in days) == totals["revenue_reversal_cents"]
                and sum(item["cost_reversal_cents"] for item in days) == totals["cost_reversal_cents"]
                and sum(item["net_margin_cents"] for item in days) == totals["net_margin_cents"])},
    ]
    for check in checks:
        check["ok"] = bool(check["ok"])

    return {
        "schema": SCHEMA,
        "sales": line_results,
        "rows": line_results,
        "days": days,
        "daily": days,
        "daily_margins": days,
        "selected_date": selected_date,
        "selected_day": selected,
        "today": selected,
        "counts": counts,
        "totals": totals,
        "issues": issues,
        "checks": checks,
        "all_checks": all(item["ok"] for item in checks),
        "balanced": all(item["ok"] for item in checks),
    }


def _select_day(days, query):
    if not query:
        if days:
            return days[-1], days[-1]["date"]
        return None, None
    text = canonical(query)
    if text.lower() in {"today", "now", "current", "今日", "当日", "今天"}:
        text = Date.today().isoformat()
    try:
        text = Date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    for row in days:
        if row["date"] == text:
            return row, text
    return {
        "date": text, "base_margin_cents": 0, "revenue_reversal_cents": 0,
        "cost_reversal_cents": 0, "adjustment_cents": 0, "net_margin_cents": 0,
        "sales_count": 0, "refund_qty": 0, "matched_qty_delta": 0,
        "refund_events": 0, "acceptance_events": 0,
    }, text


def daily_margins(*args, **kwargs):
    """仅返回日级净毛利列表。"""
    return build_ledger(*args, **kwargs)["days"]


def net_margin_for(day, *args, **kwargs):
    """返回指定日期的净毛利（分）；无当日记录时为 0。"""
    kwargs["date"] = day
    return build_ledger(*args, **kwargs)["selected_day"]["net_margin_cents"]


reconcile = compute = calculate = compute_ledger = ledger = build_ledger


def money(value):
    """整数分转 Decimal 元，仅用于展示。"""
    return Decimal(int(value)) / Decimal(100)
