# 接口

- `GET /`：工作台。
- `GET /api/catalog`：阶段、字段和样例。
- `POST /api/stages/{stage}`：按阶段返回处理结果。
- `POST /api/runs`：执行完整批次并保存草稿。
- `GET /api/runs/{id}`：读取批次。
- `POST /api/runs/{id}/review`：签发或暂缓，提交 `decision`、`comment`、`expected_revision`。
- `GET /api/runs/{id}/evidence`：下载已签发证据。
- `GET /api/examples/{profile}`：下载样例。
- `GET /healthz`：健康检查。

## 退货跨日记账

- `POST /api/returns/ledger`：按请求体中的销售行与退款/验收/作废凭据计算行级与日级净毛利；
  传 `date` 时同时返回 `selected_day`。`stored=true` 使用本地已入库凭据。
- `GET|POST /api/returns/day?date=YYYY-MM-DD`：返回指定日期（`stored=true` 时取本地库）的当日净毛利。
- `POST /api/returns/ingest`：把销售行与凭据写入本地库；同凭据重放不重复记账，
  验收单可原位改为作废。不传凭据时导入 `sample_data/returns.json`。
- `GET /api/returns/state`：读取本地库中的销售行、凭据与当前账表。
- `DELETE /api/returns/state`：清空本地退货凭据。
- `POST /api/returns/runs`：以退货账生成批次草稿，同一组凭据（顺序无关）重跑返回同一批次；
  签发、版本号与证据流程与普通批次一致。

字段与规则见 `docs/RETURNS.md`。
