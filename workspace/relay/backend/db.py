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

-- ============================================================
-- 岗位权限与双人复核（值班操作审批流）
--   * accounts      值班账号与三类岗位权限（observe/dispatch/archive_review）
--   * approvals     敏感指令待批事项（带资源版本与请求指纹）
--   * approval_decisions  完整决策记录（申请/批准/拒绝/失效/执行成败，只追加）
-- ============================================================
CREATE TABLE IF NOT EXISTS accounts(
  username    TEXT PRIMARY KEY,
  display_name TEXT NOT NULL DEFAULT '',
  roles       TEXT NOT NULL DEFAULT '',   -- 逗号分隔: observe,dispatch,archive_review
  active      INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS approvals(
  id               TEXT PRIMARY KEY,        -- AP-<递增>-<hash8>
  action           TEXT NOT NULL,           -- package_import|manual_retry|ack_terminal|batch_seal
  request_hash     TEXT NOT NULL,           -- 请求指纹：canonical(动作+参数+申请人) 的 sha256
  resource_ref     TEXT NOT NULL DEFAULT '',-- 目标资源（batch_ref；执行包导入为其 batch_ref）
  resource_version TEXT NOT NULL DEFAULT '',-- 申请时目标状态版本（canonical sha256）
  payload          TEXT NOT NULL,           -- 待执行指令原文 JSON
  requested_by     TEXT NOT NULL,
  requested_at     REAL NOT NULL,
  status           TEXT NOT NULL,           -- pending|approved|rejected|invalid|executed|execute_failed
  decided_by       TEXT NOT NULL DEFAULT '',
  decided_at       REAL,
  expires_at       REAL NOT NULL,
  invalid_reason   TEXT NOT NULL DEFAULT '',
  execution_result TEXT NOT NULL DEFAULT '',-- 批准后执行结果 JSON（幂等回放来源）
  executed_at      REAL
);

CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, requested_at);

-- 完整决策记录：申请/批准/拒绝/失效/执行成功/执行失败各一行，只追加永不修改
CREATE TABLE IF NOT EXISTS approval_decisions(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL NOT NULL,
  approval_id  TEXT NOT NULL,
  action       TEXT NOT NULL DEFAULT '',
  kind         TEXT NOT NULL,              -- requested|approved|rejected|invalidated|executed|execute_failed
  actor        TEXT NOT NULL DEFAULT 'system',
  detail       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_decisions_approval
  ON approval_decisions(approval_id, id);
"""

# 三类岗位权限
ROLES = ("observe", "dispatch", "archive_review")
ROLE_LABELS = {
    "observe": "观察", "dispatch": "调度", "archive_review": "归档审查",
}
# 敏感指令 → 申请所需权限 / 批准所需权限
ACTION_ROLES = {
    "package_import": "dispatch",
    "manual_retry": "dispatch",
    "ack_terminal": "dispatch",
    "batch_seal": "archive_review",
}
ACTION_LABELS = {
    "package_import": "执行包导入",
    "manual_retry": "失败重试",
    "ack_terminal": "终态解除重投",
    "batch_seal": "批次封存",
}
# 默认待批事项有效期（秒）；可由 RELAY_APPROVAL_TTL 覆盖
DEFAULT_APPROVAL_TTL = 15 * 60


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
        # 资源版本（revision）：资源变化的事务在提交前单调 +1；
        # 提交前回调（precommit）据此让目标状态已变化的未决事项立即失效。
        self._revision_dirty = False
        self._approvals_created_tx = set()  # 本事务新建的待批事项（不被本事务失效）
        self._approvals_executing = set()   # 正在批准执行的事项（其自身提交不失效它）
        self.precommit = []                 # 回调列表 fn(db)->None，在提交前、锁内执行
        self.conn.commit()

    def _migrate(self):
        """轻量迁移：为既有库补充封存归档所需的列（表由 SCHEMA 幂等创建）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(packages)")}
        if "archived" not in cols:
            self.conn.execute(
                "ALTER TABLE packages ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "archived_at" not in cols:
            self.conn.execute("ALTER TABLE packages ADD COLUMN archived_at REAL")
        self._migrate_approvals_unique()

    def _migrate_approvals_unique(self):
        """旧版 approvals.request_hash 带 UNIQUE；失效/执行失败后允许同指纹重提，
        需要去掉唯一约束（SQLite 通过 建临时表→拷数据→换表 完成）。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(approvals)")}
        if not cols:
            return  # 全新库（SCHEMA 建的表已无 UNIQUE）
        need_rebuild = False
        for idx in self.conn.execute("PRAGMA index_list(approvals)").fetchall():
            if idx["unique"] and idx["origin"] == "u":
                # 表级/列级 UNIQUE 自动索引（主键 origin='pk' 排除）
                info = self.conn.execute(
                    f"PRAGMA index_info({idx['name']})").fetchall()
                if [i["name"] for i in info] == ["request_hash"]:
                    need_rebuild = True
                    break
        if need_rebuild:
            self.conn.executescript(
                "ALTER TABLE approvals RENAME TO approvals_old_uniq;"
                "CREATE TABLE approvals("
                " id TEXT PRIMARY KEY, action TEXT NOT NULL,"
                " request_hash TEXT NOT NULL, resource_ref TEXT NOT NULL DEFAULT '',"
                " resource_version TEXT NOT NULL DEFAULT '', payload TEXT NOT NULL,"
                " requested_by TEXT NOT NULL, requested_at REAL NOT NULL,"
                " status TEXT NOT NULL, decided_by TEXT NOT NULL DEFAULT '',"
                " decided_at REAL, expires_at REAL NOT NULL,"
                " invalid_reason TEXT NOT NULL DEFAULT '',"
                " execution_result TEXT NOT NULL DEFAULT '', executed_at REAL);"
                "INSERT INTO approvals SELECT id,action,request_hash,resource_ref,"
                "resource_version,payload,requested_by,requested_at,status,"
                "decided_by,decided_at,expires_at,invalid_reason,execution_result,"
                "executed_at FROM approvals_old_uniq;"
                "DROP TABLE approvals_old_uniq;"
                "CREATE INDEX IF NOT EXISTS idx_approvals_status"
                " ON approvals(status, requested_at);")

    # ---- 资源版本 / 事务钩子 ----
    def revision(self):
        """当前资源版本号（单调递增整数；全新库为 0）。"""
        return int(self.get_meta("revision", "0") or 0)

    def mark_revision(self):
        """标记本事务改变了资源状态：提交时 revision +1 并触发未决事项失效检查。"""
        self._revision_dirty = True

    def approval_created(self, approval_id):
        """登记本事务内新建的待批事项，使其不被本事务的资源变化失效。"""
        self._approvals_created_tx.add(approval_id)

    def commit(self):
        # 资源版本先推进，再跑提交前回调（审批失效检查能读到新 revision）
        if self._revision_dirty:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('revision',?1) "
                "ON CONFLICT(key) DO UPDATE SET value=?1",
                (str(self.revision() + 1),))
        for fn in self.precommit:
            fn(self)
        self.conn.commit()
        self._revision_dirty = False
        self._approvals_created_tx = set()
        self._approvals_executing = set()

    def rollback(self):
        self.conn.rollback()
        self._revision_dirty = False
        self._approvals_created_tx = set()
        self._approvals_executing = set()

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

    def decision(self, approval_id, kind, actor="system", action="", detail=None):
        """追加一条完整决策记录（只追加；审批流全过程留痕）。"""
        self.conn.execute(
            "INSERT INTO approval_decisions(ts,approval_id,action,kind,actor,detail)"
            " VALUES(?,?,?,?,?,?)",
            (time.time(), approval_id, action, kind, actor,
             json.dumps(detail if detail is not None else {}, ensure_ascii=False)),
        )
