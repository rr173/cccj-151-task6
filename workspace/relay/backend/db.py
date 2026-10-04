"""离线车队回执中继 —— SQLite 持久化层。

设计要点：
  * 执行包(packages)一经导入**不可变**：同 batch_ref 不同内容一律冲突拒绝。
  * 每个批次一条游标(cursors.next_seq)：只有 seq == next_seq 的回执可以转发，
    大于游标的乱序回执落 receipts 表持久缓冲；转发成功后游标才前推。
  * 终态拒绝(rejected_terminal)不移动游标 → 后续任何回执都无法越过该项。
  * dispatching 是带租约(lease_until)的派发态：进程崩溃/重启后租约过期，
    行被安全回收为 queued 重新派发；forwarded 永不被再次选中（恰好一次效果）。
  * 所有写事务在 self.lock 内串行（HTTP 线程与后台转发线程共享同一把锁）。

仅依赖 Python 标准库（sqlite3）。
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 不可变执行批次包（现场终端下载的对象）
CREATE TABLE IF NOT EXISTS packages(
  batch_ref     TEXT PRIMARY KEY,          -- 批次唯一编号（编排系统侧批次 id）
  package_id    TEXT NOT NULL DEFAULT '',
  batch_summary TEXT NOT NULL,             -- 批次摘要（回执必须携带并与之匹配）
  seq_base       INTEGER NOT NULL DEFAULT 1,-- 回执序号起点（通常为 1）
  content_hash  TEXT NOT NULL UNIQUE,      -- 规范化内容哈希：相同导入幂等、不同内容冲突
  payload       TEXT NOT NULL,             -- 原始包 JSON（不可变存档）
  source        TEXT NOT NULL DEFAULT '',
  imported_at   REAL NOT NULL
);

-- 每批次转发游标
CREATE TABLE IF NOT EXISTS cursors(
  batch_ref TEXT PRIMARY KEY,
  next_seq  INTEGER NOT NULL,              -- 下一个允许转发的序号
  FOREIGN KEY(batch_ref) REFERENCES packages(batch_ref)
);

-- 回执（按批次内序号严格有序）
CREATE TABLE IF NOT EXISTS receipts(
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_ref      TEXT NOT NULL,
  seq            INTEGER NOT NULL,
  receipt_no     TEXT NOT NULL,            -- 现场回执编号
  receipt_type   TEXT NOT NULL,            -- departed | failed_to_depart | arrived
  content_hash   TEXT NOT NULL,            -- 回执规范化内容哈希（判重/判冲突）
  payload        TEXT NOT NULL,            -- 回执原文 JSON
  state          TEXT NOT NULL,            -- buffered | queued | dispatching |
                                           -- forwarded | retry_wait | rejected_terminal
  attempts       INTEGER NOT NULL DEFAULT 0,
  last_error     TEXT NOT NULL DEFAULT '',
  result_code    TEXT NOT NULL DEFAULT '',  -- ack / 终态拒绝原因
  result_detail  TEXT NOT NULL DEFAULT '',  -- 主系统响应留痕
  next_retry_at  REAL,
  lease_until    REAL,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  forwarded_at   REAL,
  rejected_at    REAL,
  UNIQUE(batch_ref, seq),
  UNIQUE(batch_ref, receipt_no)
);

-- 被拒绝的冲突/非法回执（只追加审计，不推进任何游标）
CREATE TABLE IF NOT EXISTS conflicts(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          REAL NOT NULL,
  batch_ref   TEXT NOT NULL DEFAULT '',
  seq         INTEGER,
  receipt_no  TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL,               -- conflict_seq | conflict_receipt_no |
                                           -- summary_mismatch | unknown_batch | malformed
  payload     TEXT NOT NULL DEFAULT '',
  detail      TEXT NOT NULL DEFAULT ''
);

-- 审计流（导入、冲突、终态拒绝、转发成功、手动重试、重启恢复等）
CREATE TABLE IF NOT EXISTS audit_log(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         REAL NOT NULL,
  kind       TEXT NOT NULL,
  batch_ref  TEXT NOT NULL DEFAULT '',
  seq        INTEGER,
  receipt_no TEXT NOT NULL DEFAULT '',
  actor      TEXT NOT NULL DEFAULT 'system',
  detail     TEXT NOT NULL DEFAULT ''
);

-- 只读归档：批次封存时生成的内容清单 + 校验摘要 + 明细快照（热库空间回收的去处）
CREATE TABLE IF NOT EXISTS archives(
  batch_ref     TEXT PRIMARY KEY,
  package_id    TEXT NOT NULL DEFAULT '',
  batch_summary TEXT NOT NULL DEFAULT '',
  digest        TEXT NOT NULL,              -- 校验摘要：sha256(规范化 清单+全部明细+审计时间线)
  manifest      TEXT NOT NULL,              -- 内容清单 JSON（每条回执的序号/编号/哈希等）
  payload       TEXT NOT NULL,              -- 完整归档 JSON（执行包+回执明细+原审计时间线）
  receipt_count INTEGER NOT NULL DEFAULT 0,
  audit_count   INTEGER NOT NULL DEFAULT 0,
  freed_bytes   INTEGER NOT NULL DEFAULT 0, -- 封存时从热库回收的字节数
  sealed_at     REAL NOT NULL,
  sealed_by     TEXT NOT NULL DEFAULT 'system',
  note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_receipts_dispatch
  ON receipts(state, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_receipts_batch ON receipts(batch_ref, seq);

-- 值班账号与岗位权限。roles 为 JSON 数组：observe | dispatch | archive_review。
CREATE TABLE IF NOT EXISTS accounts(
  username      TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL DEFAULT '',
  roles         TEXT NOT NULL DEFAULT '[]',
  active        INTEGER NOT NULL DEFAULT 1,
  role_version  INTEGER NOT NULL DEFAULT 1,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  updated_by    TEXT NOT NULL DEFAULT 'system'
);

-- 敏感指令双人复核待办/决策主表。请求指纹全局唯一，重复请求永远返回同一事项。
CREATE TABLE IF NOT EXISTS approvals(
  id                    TEXT PRIMARY KEY,
  action                TEXT NOT NULL,       -- package_import | retry | terminal_ack | batch_seal
  resource_ref          TEXT NOT NULL DEFAULT '',
  request_fingerprint   TEXT NOT NULL UNIQUE,
  request_payload       TEXT NOT NULL,
  resource_version      TEXT NOT NULL,       -- 创建时目标资源版本（JSON）
  resource_snapshot     TEXT NOT NULL,       -- 可重算/比对的完整状态摘要（JSON）
  state                 TEXT NOT NULL,       -- pending | executing | approved | rejected |
                                             -- invalid | expired | failed
  invalid_reason        TEXT NOT NULL DEFAULT '',
  applicant             TEXT NOT NULL,
  applicant_roles       TEXT NOT NULL DEFAULT '[]',
  applicant_role_version INTEGER NOT NULL,
  required_role         TEXT NOT NULL,
  approver              TEXT NOT NULL DEFAULT '',
  decision_note         TEXT NOT NULL DEFAULT '',
  result_payload        TEXT NOT NULL DEFAULT '',
  error_code            TEXT NOT NULL DEFAULT '',
  error_message         TEXT NOT NULL DEFAULT '',
  created_at            REAL NOT NULL,
  expires_at            REAL NOT NULL,
  decision_started_at   REAL,
  decided_at            REAL,
  executed_at          REAL
);

CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_applicant ON approvals(applicant, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_approver ON approvals(approver, decided_at);

-- 每次批准/拒绝尝试均追加；effective=1 的记录至多一条，重复/并发尝试保留为非生效记录。
CREATE TABLE IF NOT EXISTS approval_decisions(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id   TEXT NOT NULL,
  ts           REAL NOT NULL,
  approver     TEXT NOT NULL,
  decision     TEXT NOT NULL,                -- approve | reject
  effective    INTEGER NOT NULL DEFAULT 0,
  result       TEXT NOT NULL DEFAULT '',     -- effective | replay | self_approval |
                                             -- unauthorized | stale | invalid | conflict ...
  detail       TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(request_id) REFERENCES approvals(id)
);
CREATE INDEX IF NOT EXISTS idx_approval_decisions_req ON approval_decisions(request_id, id);
"""

DEFAULT_ACCOUNTS = (
    ("admin", "值班管理员", ["observe", "dispatch", "archive_review"]),
    ("deputy", "副值班管理员", ["observe", "dispatch", "archive_review"]),
    ("dispatcher", "调度员", ["observe", "dispatch"]),
    ("reviewer", "归档审查员", ["observe", "archive_review"]),
    ("observer", "观察员", ["observe"]),
)


class RelayDB:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._seed_default_accounts()
        self.conn.commit()

    def _seed_default_accounts(self):
        exists = self.conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"]
        if exists:
            return
        t = time.time()
        for username, display_name, roles in DEFAULT_ACCOUNTS:
            self.conn.execute(
                "INSERT INTO accounts(username,display_name,roles,active,role_version,"
                "created_at,updated_at,updated_by) VALUES(?,?,?,1,1,?,?,?)",
                (username, display_name, json.dumps(roles, ensure_ascii=False),
                 t, t, "bootstrap"))
        self.audit("accounts_bootstrapped", actor="bootstrap",
                   detail={"accounts": [x[0] for x in DEFAULT_ACCOUNTS]})

    def _migrate(self):
        """轻量迁移：为既有库补充封存归档所需的列（表由 SCHEMA 幂等创建）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(packages)")}
        if "archived" not in cols:
            self.conn.execute(
                "ALTER TABLE packages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "archived_at" not in cols:
            self.conn.execute("ALTER TABLE packages ADD COLUMN archived_at REAL")

    # ---- meta ----
    def get_meta(self, key, default=None):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def audit(self, kind, batch_ref="", seq=None, receipt_no="", actor="system", detail=None):
        self.conn.execute(
            "INSERT INTO audit_log(ts,kind,batch_ref,seq,receipt_no,actor,detail)"
            " VALUES(?,?,?,?,?,?,?)",
            (time.time(), kind, batch_ref, seq, receipt_no, actor,
             json.dumps(detail if detail is not None else {}, ensure_ascii=False)),
        )

    def add_conflict(self, reason, receipt=None, detail=""):
        receipt = receipt or {}
        self.conn.execute(
            "INSERT INTO conflicts(ts,batch_ref,seq,receipt_no,content_hash,payload,reason,detail)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), str(receipt.get("batch_ref", "")),
             receipt.get("seq") if isinstance(receipt.get("seq"), int) else None,
             str(receipt.get("receipt_no", "")),
             str(receipt.get("_content_hash", "")),
             json.dumps(receipt.get("_raw", receipt), ensure_ascii=False),
             reason, detail),
        )

    def commit(self):
        self.conn.commit()
