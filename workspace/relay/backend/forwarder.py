"""后台转发器：严格按批次序号连续转发，保证顺序与恰好一次效果。

并发模型：单工作线程 + 全局 db 锁。回调是网络 I/O，采用"领取租约 → 释放锁做 I/O
→ 重新加锁终局"两阶段：即使两个周期重叠，也只有持有租约的那一次调用会被终局，
而 forwarded 行永不再次领取，因此已成功的回调不会被重复调用。

每批次每周期只派发游标所在的 seq（cursors.next_seq 对应行）：
  * 成功 ack      → forwarded, 游标 +1，原 buffered 的下一序号自动转 queued（补齐连续转发）；
  * 瞬时 retry    → retry_wait + 指数退避，游标不动；
  * terminal      → rejected_terminal，游标冻结；其后 buffered 项永不被领取（无法越过）。
"""
import json
import os
import random
import threading
import time

import core


def backoff_delay(attempts, base=None, cap=None):
    base = float(base if base is not None else os.environ.get("RELAY_BACKOFF_BASE", "1"))
    cap = float(cap if cap is not None else os.environ.get("RELAY_BACKOFF_CAP", "60"))
    return min(cap, base * (2 ** max(0, attempts - 1))) + random.uniform(0, base * 0.2)


class Forwarder:
    def __init__(self, db, adapter, lease_seconds=None, interval=None,
                 backoff_base=None, backoff_cap=None):
        self.db = db
        self.adapter = adapter
        self.lease = float(lease_seconds if lease_seconds is not None
                           else os.environ.get("RELAY_LEASE_SECONDS", "30"))
        self.interval = float(interval if interval is not None
                              else os.environ.get("RELAY_TICK_SECONDS", "1"))
        self.backoff_base = float(backoff_base if backoff_base is not None
                                  else os.environ.get("RELAY_BACKOFF_BASE", "1"))
        self.backoff_cap = float(backoff_cap if backoff_cap is not None
                                 else os.environ.get("RELAY_BACKOFF_CAP", "60"))
        self._wake = threading.Event()
        self._thread = None
        self._stop = False
        self.stats = {"cycles": 0, "dispatched": 0, "acked": 0,
                      "retries": 0, "terminal": 0}

    # ---- 生命周期 ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name="relay-forwarder",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def kick(self):
        """新回执入账后唤醒，立即尝试转发（不必等下一个 tick）。"""
        self._wake.set()

    # ---- 主循环 ----
    def _run(self):
        while not self._stop:
            try:
                self.cycle()
            except Exception as e:  # noqa: BLE001 —— 后台线程绝不能死
                with self.db.lock:
                    self.db.audit("forwarder_error", detail={"error": repr(e)})
                    self.db.commit()
            self._wake.wait(self.interval)
            self._wake.clear()

    def cycle(self):
        """一个派发周期：领取一个到期项并完成回调终局。返回派发的 (batch,seq) 或 None。"""
        self.stats["cycles"] += 1
        claim = self._claim_next()
        if not claim:
            # 到期的 retry_wait 行若到点，_claim_next 已包含；无任务则回收过期租约
            self._reap_expired_leases()
            return None
        pkg_row, rcp_row, idem = claim
        self.stats["dispatched"] += 1

        package = json.loads(pkg_row["payload"])
        raw = json.loads(rcp_row["payload"])
        # 适配器统一读规范字段（现场原文可能只带 type）
        receipt = {**raw,
                   "batch_ref": rcp_row["batch_ref"], "seq": rcp_row["seq"],
                   "receipt_no": rcp_row["receipt_no"],
                   "receipt_type": rcp_row["receipt_type"]}
        result = self.adapter.deliver(package, receipt, idem)

        with self.db.lock:
            self._finalize(rcp_row, result, idem)
        return rcp_row["batch_ref"], rcp_row["seq"]

    # ---- 领取（事务内：状态机 + 租约）----
    def _claim_next(self):
        with self.db.lock:
            self._reap_expired_leases_locked()
            t = time.time()
            cursors = self.db.conn.execute("SELECT batch_ref,next_seq FROM cursors").fetchall()
            for c in cursors:
                batch_ref, seq = c["batch_ref"], c["next_seq"]
                r = self.db.conn.execute(
                    "SELECT * FROM receipts WHERE batch_ref=? AND seq=?",
                    (batch_ref, seq)).fetchone()
                if not r:
                    continue  # 缺口：等补齐
                if r["state"] == "queued" or (
                        r["state"] == "retry_wait"
                        and r["next_retry_at"] is not None and r["next_retry_at"] <= t):
                    attempts = r["attempts"] + 1
                    self.db.conn.execute(
                        "UPDATE receipts SET state='dispatching',attempts=?,"
                        "lease_until=?,updated_at=? WHERE id=? AND state!=?",
                        (attempts, t + self.lease, t, r["id"], "forwarded"))
                    self.db.audit("dispatch", batch_ref, seq=seq,
                                  receipt_no=r["receipt_no"],
                                  detail={"attempt": attempts,
                                          "adapter": self.adapter.name,
                                          "idempotency_key": self._idem(r)})
                    self.db.commit()
                    pkg = self.db.conn.execute(
                        "SELECT * FROM packages WHERE batch_ref=?",
                        (batch_ref,)).fetchone()
                    fresh = self.db.conn.execute(
                        "SELECT * FROM receipts WHERE id=?", (r["id"],)).fetchone()
                    return pkg, fresh, self._idem(fresh)
            return None

    @staticmethod
    def _idem(r):
        return f"{r['batch_ref']}:{r['seq']}:{r['content_hash'][:16]}"

    # ---- 终局（事务内）----
    def _finalize(self, rcp_row, result, idem):
        t = time.time()
        r = self.db.conn.execute(
            "SELECT * FROM receipts WHERE id=?", (rcp_row["id"],)).fetchone()
        # CAS：只有仍停留在"本次领取的 dispatching 租约"上才允许终局。
        # 若租约已过期被回收并由新派发接管（attempts 已增加），放弃本次迟到结果，
        # 避免旧回调的 ack/timeout 覆盖新派发的状态。
        if r is None or r["state"] != "dispatching" \
                or r["attempts"] != rcp_row["attempts"]:
            self.db.conn.rollback()
            self.db.audit("finalize_stale", (r["batch_ref"] if r else ""),
                          seq=(r["seq"] if r else None),
                          detail={"got_attempts": (r["attempts"] if r else None),
                                  "claim_attempts": rcp_row["attempts"]})
            self.db.commit()
            return

        if result.kind == "ack":
            self.db.conn.execute(
                "UPDATE receipts SET state='forwarded',result_code=?,result_detail=?,"
                "last_error='',next_retry_at=NULL,lease_until=NULL,forwarded_at=?,"
                "updated_at=? WHERE id=?",
                (result.code or "ack", result.detail[:2000], t, t, r["id"]))
            # 仅当该行正是游标位置时推进游标（严格顺序，不跳过任何项）
            cur = self.db.conn.execute(
                "SELECT next_seq FROM cursors WHERE batch_ref=?",
                (r["batch_ref"],)).fetchone()
            advanced = 0
            if cur and cur["next_seq"] == r["seq"]:
                new_next = r["seq"] + 1
                self.db.conn.execute(
                    "UPDATE cursors SET next_seq=? WHERE batch_ref=?",
                    (new_next, r["batch_ref"]))
                advanced = 1
                # 补齐后连续转发：紧跟其后的 buffered 项全部转 queued
                self.db.conn.execute(
                    "UPDATE receipts SET state='queued',updated_at=?"
                    " WHERE batch_ref=? AND seq=? AND state='buffered'",
                    (t, r["batch_ref"], new_next))
            self.db.audit("forward_ok", r["batch_ref"], seq=r["seq"],
                          receipt_no=r["receipt_no"],
                          detail={"idempotency_key": idem,
                                  "adapter": self.adapter.name,
                                  "cursor_advanced": advanced})
            self.stats["acked"] += 1

        elif result.kind == "terminal":
            self.db.conn.execute(
                "UPDATE receipts SET state='rejected_terminal',result_code=?,"
                "result_detail=?,last_error=?,next_retry_at=NULL,lease_until=NULL,"
                "rejected_at=?,updated_at=? WHERE id=?",
                (result.code or "rejected", result.detail[:2000],
                 result.detail[:500], t, t, r["id"]))
            # 游标不动：后续 buffered 项无法越过
            self.db.audit("forward_terminal_reject", r["batch_ref"], seq=r["seq"],
                          receipt_no=r["receipt_no"],
                          detail={"reason": result.code, "detail": result.detail[:500],
                                  "cursor_frozen_at": r["seq"]})
            self.stats["terminal"] += 1

        else:  # retry —— 瞬时失败，退避后安全重试
            delay = backoff_delay(r["attempts"], self.backoff_base, self.backoff_cap)
            self.db.conn.execute(
                "UPDATE receipts SET state='retry_wait',last_error=?,result_code=?,"
                "result_detail=?,next_retry_at=?,lease_until=NULL,updated_at=? WHERE id=?",
                (f"{result.code}: {result.detail}"[:500], result.code,
                 result.detail[:2000], t + delay, t, r["id"]))
            self.db.audit("forward_retry", r["batch_ref"], seq=r["seq"],
                          receipt_no=r["receipt_no"],
                          detail={"reason": result.code, "attempt": r["attempts"],
                                  "retry_after_seconds": round(delay, 2)})
            self.stats["retries"] += 1
        self.db.commit()
        self.kick()  # 立即尝试下一个就绪序号（连续转发）

    # ---- 过期租约回收 ----
    def _reap_expired_leases(self):
        with self.db.lock:
            self._reap_expired_leases_locked()

    def _reap_expired_leases_locked(self):
        t = time.time()
        rows = self.db.conn.execute(
            "SELECT id,batch_ref,seq,receipt_no,attempts,lease_until FROM receipts"
            " WHERE state='dispatching' AND lease_until IS NOT NULL AND lease_until<?",
            (t,)).fetchall()
        for r in rows:
            self.db.conn.execute(
                "UPDATE receipts SET state='queued',lease_until=NULL,"
                "next_retry_at=NULL,updated_at=? WHERE id=?", (t, r["id"]))
            self.db.audit("lease_reclaimed", r["batch_ref"], seq=r["seq"],
                          receipt_no=r["receipt_no"],
                          detail={"attempts": r["attempts"]})
        if rows:
            self.db.commit()
