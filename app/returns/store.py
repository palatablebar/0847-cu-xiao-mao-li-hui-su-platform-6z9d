"""退货凭据与销售行的本地持久化。

退款单和验收单可以乱序、分批到达。每次记账都从完整凭据集合重建时间线，
因此晚到的验收单会把此前未冲回的成本补记在验收日。

* 凭据以 credential_id 为主键；同凭据同内容重复提交不重复记账；
* 验收单从有效改为作废时原位更新，保留原验收日期；
* 同凭据不同内容保留首次提交并记冲突。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.returns import engine
from app.returns.engine import document_kind, document_signature


class ReturnsStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=8)
        connection.row_factory = sqlite3.Row
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS return_documents(
              credential TEXT PRIMARY KEY,
              kind TEXT NOT NULL,
              sale_id TEXT NOT NULL DEFAULT '',
              signature TEXT NOT NULL,
              payload TEXT NOT NULL,
              received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS return_sales(
              sale_id TEXT PRIMARY KEY,
              signature TEXT NOT NULL,
              payload TEXT NOT NULL,
              received_at TEXT NOT NULL
            );
            ''')

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    # -- 读取 -------------------------------------------------------------
    def documents(self, kind=None):
        with self.connect() as db:
            if kind:
                rows = db.execute("SELECT payload FROM return_documents WHERE kind=? "
                                  "ORDER BY received_at, credential", (kind,)).fetchall()
            else:
                rows = db.execute("SELECT payload FROM return_documents "
                                  "ORDER BY received_at, credential").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def sales(self):
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM return_sales ORDER BY sale_id").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def snapshot(self):
        return {"sales": self.sales(), "documents": self.documents()}

    # -- 写入 -------------------------------------------------------------
    def ingest_documents(self, rows):
        result = {"input": 0, "ingested": 0, "replayed": 0, "updated": 0,
                  "conflicts": 0, "isolated": 0, "issues": []}
        for index, raw in enumerate(rows):
            result["input"] += 1
            if not isinstance(raw, dict):
                result["isolated"] += 1
                result["issues"].append({"index": index, "credential": "", "code": "BAD_DOCUMENT"})
                continue
            credential = engine.credential_of(raw)
            kind = document_kind(raw)
            if not credential:
                result["isolated"] += 1
                result["issues"].append({"index": index, "credential": "", "code": "DOC_ID_MISSING"})
                continue
            if not kind:
                result["isolated"] += 1
                result["issues"].append({"index": index, "credential": credential,
                                          "code": "UNKNOWN_DOC_KIND"})
                continue
            signature = document_signature(raw, kind)
            with self.connect() as db:
                existing = db.execute("SELECT * FROM return_documents WHERE credential=?",
                                      (credential,)).fetchone()
            if existing is None:
                with self.connect() as db:
                    db.execute("INSERT INTO return_documents"
                               "(credential,kind,sale_id,signature,payload,received_at) "
                               "VALUES(?,?,?,?,?,?)",
                               (credential, kind, engine._text(raw, engine.SALE_KEYS),
                                signature, json.dumps(raw, ensure_ascii=False), self._now()))
                result["ingested"] += 1
                continue
            if existing["signature"] == signature:
                result["replayed"] += 1
                result["issues"].append({"index": index, "credential": credential,
                                          "code": "REPLAYED_DOC"})
                continue
            previous = json.loads(existing["payload"])
            outcome = self._merge_existing(existing["kind"], previous, kind, raw)
            if outcome is not None:
                if outcome == "replayed":
                    result["replayed"] += 1
                    result["issues"].append({"index": index, "credential": credential,
                                              "code": "REPLAYED_DOC"})
                    continue
                merged = outcome
                with self.connect() as db:
                    db.execute("UPDATE return_documents SET kind=?, sale_id=?, signature=?, payload=?, "
                               "received_at=? WHERE credential=?",
                               (merged[0], engine._text(merged[1], engine.SALE_KEYS),
                                document_signature(merged[1], merged[0]),
                                json.dumps(merged[1], ensure_ascii=False), existing["received_at"],
                                credential))
                result["updated"] += 1
                result["issues"].append({"index": index, "credential": credential,
                                          "code": "ACCEPTANCE_VOIDED"})
                continue
            result["conflicts"] += 1
            result["issues"].append({"index": index, "credential": credential,
                                      "code": "DOC_CONFLICT"})
        return result

    @staticmethod
    def _merge_existing(previous_kind, previous, kind, raw):
        """验收单可原位转为作废；重复作废视为重放；其余冲突返回 None。"""
        if previous_kind != "acceptance" or kind != "acceptance":
            return None
        if not engine._same_acceptance(previous, raw):
            return None
        previous_status, current_status = engine._status(previous), engine._status(raw)
        if previous_status in engine.VOID_STATUS:
            if current_status in engine.VOID_STATUS:
                return "replayed"
            return None
        if current_status not in engine.VOID_STATUS:
            return None
        merged = dict(raw)
        merged["accepted_date"] = engine._text(previous, engine.ACCEPT_DATE_KEYS)
        if any(name in raw and engine.canonical(raw[name]) for name in
               engine.ACCEPT_STRICT_KEYS):
            merged["accepted_date"] = engine._text(raw, engine.ACCEPT_STRICT_KEYS)
        return "acceptance", merged

    def ingest_sales(self, rows):
        result = {"input": 0, "ingested": 0, "replayed": 0, "conflicts": 0,
                  "isolated": 0, "issues": []}
        for index, raw in enumerate(rows):
            result["input"] += 1
            if not isinstance(raw, dict):
                result["isolated"] += 1
                result["issues"].append({"index": index, "sale_id": "", "code": "BAD_SALE"})
                continue
            sale_id = engine._text(raw, engine.SALE_KEYS)
            if not sale_id:
                result["isolated"] += 1
                result["issues"].append({"index": index, "sale_id": "", "code": "SALE_ID_MISSING"})
                continue
            signature = document_signature(raw, "sale")
            with self.connect() as db:
                existing = db.execute("SELECT * FROM return_sales WHERE sale_id=?",
                                      (sale_id,)).fetchone()
            if existing is None:
                with self.connect() as db:
                    db.execute("INSERT INTO return_sales(sale_id,signature,payload,received_at) "
                               "VALUES(?,?,?,?)",
                               (sale_id, signature, json.dumps(raw, ensure_ascii=False), self._now()))
                result["ingested"] += 1
            elif existing["signature"] == signature:
                result["replayed"] += 1
            else:
                result["conflicts"] += 1
                result["issues"].append({"index": index, "sale_id": sale_id, "code": "SALE_CONFLICT"})
        return result

    def clear(self):
        with self.connect() as db:
            db.execute("DELETE FROM return_documents")
            db.execute("DELETE FROM return_sales")
