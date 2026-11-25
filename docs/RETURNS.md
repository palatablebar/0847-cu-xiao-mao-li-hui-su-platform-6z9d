# 退货跨日记账

促销退货常常跨日：退款到账与验收入库不是同一天，单据也可能乱序到达。
本模块在原有"出货顺序与行毛利"之上，单独维护退货对收入与成本的影响，
让工作台能查到**退货后的当日净毛利**。

## 核心规则

1. **收入按到账日冲减。** 退款单上的到账日 `received_date` 是收入冲减日；
   退款金额优先使用 `amount_cents`（分），缺省时按原销售净收入按退货数量比例分摊。
2. **成本只在"已退款且已验收"的数量上冲回。** 同一条销售行，累计已退款数量
   与累计已验收数量取较小值，按原销售净成本比例冲回；只退款未验收当天不冲回成本。
3. **乱序到达按业务日期重建。** 先验收后退款时，成本在退款到账日冲回；
   先退款后验收时，成本在验收日补记。重算整条时间线，不依赖到达顺序。
4. **超额不入账。** 累计退款数量超过原售数量时，超出部分数量与金额都不入账，
   并给出 `EXCESS_RETURN`；退款金额超过原销售净收入同理。验收数量超过原售数量
   时，超出部分不参与成本冲回（`EXCESS_ACCEPTANCE`）。
5. **验收单作废恢复成本。** 作废单（`kind=void` 指向验收凭据，或验收单
   `status=voided` 带 `voided_date`）在作废日撤销对应的成本冲回；
   已到账的退款继续生效，收入不回滚。
6. **凭据幂等。** 同一 `credential_id` 重复提交只记一次（`REPLAYED_DOC`）；
   同凭据不同内容保留首次并标记 `DOC_CONFLICT`，不会重复记账。

## 行级与日级指标

每条销售行输出：

| 字段 | 含义 |
|---|---|
| `refunded_qty` | 入账的已退款数量（已封顶） |
| `accepted_qty` | 仍有效的已验收数量（已封顶） |
| `matched_qty` | 可冲回成本的数量（两者较小值） |
| `revenue_reversal_cents` | 收入冲减（分） |
| `cost_reversal_cents` | 成本冲回（分） |
| `net_margin_after_cents` | 原毛利 − 收入冲减 + 成本冲回 |

日级 `days[]` 按日期给出 `base_margin_cents`（当天销售毛利）、
`revenue_reversal_cents`、`cost_reversal_cents`、`adjustment_cents` 与
`net_margin_cents`。`selected_day` / `today` 是工作台"查看日期"对应的当日净毛利。

## 持久化

`app/returns/store.py` 把销售行与凭据写入 SQLite：

- 凭据以 `credential_id` 为主键，乱序、分批到达都只保留一份；
- 每次记账都从完整集合重建时间线，晚到的验收单会补记此前未冲回的成本；
- 验收单可从有效原位改为作废，保留原验收日期。

## 单据字段

单据类型由 `kind` 识别，也可由字段推断：带 `target_doc_id` 视为作废单，
带 `accepted_date` 视为验收单，带退款日期/金额视为退款单。

| 单据 | 必填 | 说明 |
|---|---|---|
| 退款 | `credential_id`、`sale_id`、`qty`、`received_date`（或 `refund_date`） | `amount_cents` 可选 |
| 验收 | `credential_id`、`sale_id`、`qty`、`accepted_date` | `status=voided` + `voided_date` 表示已作废 |
| 作废 | `credential_id`、`target_doc_id`、`voided_date`（或 `void_date`） | 指向验收凭据 |

金额为整数分；写 `amount` / `refund_amount` 时按元解析，再转成分。
