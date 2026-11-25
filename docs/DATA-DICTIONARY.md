# 字段定义

| 字段 | 类型 | 说明 |
|---|---|---|
| `sku` | text | 主键 |
| `line_qty` | int_positive | 业务字段 |
| `list_price_cents` | int | 业务字段 |
| `sell_price_cents` | int | 业务字段 |
| `supplier_fund_cents` | int | 业务字段 |
| `coupon_cents` | int | 业务字段 |
| `unit_cost_cents` | int | 业务字段 |

空字符串、`-`、`NA`、`null` 和 `未提供` 按空值处理。金额字段在整理层转为整数分或整数最小单位；日期按 ISO 日期解析；带时间的字段按本地时间解析。标识字段保持为文本，前导零不会被去除。

## 退货凭据

| 字段 | 类型 | 说明 |
|---|---|---|
| `credential_id` | text | 凭据主键；同凭据重放幂等 |
| `kind` | text | `refund` / `acceptance` / `void`，可按字段推断 |
| `sale_id` | text | 对应销售行；作废单可省略 |
| `qty` | int_positive | 退货或验收数量 |
| `received_date` | date | 退款到账日；决定收入冲减日 |
| `accepted_date` | date | 验收入库日；决定成本冲回日 |
| `amount_cents` | int | 退款金额（分）；缺省按净收入比例 |
| `amount` | decimal | 退款金额（元），无 `amount_cents` 时使用 |
| `target_doc_id` | text | 作废单指向的验收凭据 |
| `voided_date` | date | 验收单作废日；当天恢复成本 |
| `status` | text | 验收单取 `voided`/`作废` 表示已作废 |

金额以整数分为准，不使用二进制浮点。明细规则见 `docs/RETURNS.md`。
