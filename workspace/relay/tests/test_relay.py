"""离线车队回执中继 —— 端到端测试（Python 标准库 unittest）。

覆盖：
  1. 执行包导入幂等 / 同 batch_ref 不同内容不可变冲突
  2. 现场终端清单与下载、摘要不匹配拒绝
  3. 乱序回执：缺口缓冲、补齐后连续转发
  4. 完全相同回执重放返回原结果；同序号 / 同回执号内容不同拒绝且游标不动
  5. 回调瞬时失败安全重试（fail_then_ok），最终恰好一次成功
  6. 主系统终态拒绝（撤回/车辆变化/目的地变化）：游标冻结、后续不得越过
  7. 人工确认终态解除后重投成功、按序继续
  8. 进程重启恢复：dispatching 租约回收、不重复调用已 forwarded 回调
  9. 手动立即重试与审计完整性

运行：python3 relay/tests/test_relay.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))

import adapters  # noqa: E402
import server as srv  # noqa: E402
import core  # noqa: E402

SUMMARY = "波次1 · V-07 · G3 → S1（4户9人）"


def package(ref="B-001", summary=SUMMARY, seq_base=1):
    return {"batch_ref": ref, "package_id": "PKG-1", "batch_summary": summary,
            "seq_base": seq_base, "issued_at": "2026-10-02T07:30:00Z",
            "vehicle": {"code": "V-07", "seats": 11},
            "destination": {"code": "S1", "name": "城北体育馆"},
            "items": [{"household_ref": "H-1"}, {"household_ref": "H-2"}]}


def receipt(seq, no, rtype="departed", summary=SUMMARY, extra=None):
    d = {"seq": seq, "receipt_no": no, "type": rtype,
         "batch_summary": summary, "at": "2026-10-02T08:00:00Z"}
    if extra:
        d.update(extra)
    return d


def batch_upload(ref, receipts, summary=SUMMARY):
    return {"batch_ref": ref, "batch_summary": summary, "receipts": receipts}


class RelayServerHarness:
    """在随机端口上启动真实 HTTP 服务；temp DB；stub 适配器；快速转发器。"""

    def __init__(self, db_path=None, adapter=None):
        self.tmp = None
        if db_path is None:
            self.tmp = tempfile.mkdtemp(prefix="relay-test-")
            db_path = os.path.join(self.tmp, "relay.db")
        self.db_path = db_path
        self.adapter = adapter or adapters.StubCallbackAdapter()
        self.app = srv.RelayApp(db_path=db_path, adapter=self.adapter,
                                interval=0.02, lease_seconds=30,
                                backoff_base=0.03, backoff_cap=0.5)
        srv.APP = self.app  # 让 HTTP Handler 使用本实例（临时库 + 桩适配器）
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/relay/api"

    def stop(self, keep_data=False):
        self.app.forwarder.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.db.conn.close()
        if self.tmp and not keep_data:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- HTTP 帮助函数 ----
    def call(self, method, path, body=None, expect_error=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode())
            if expect_error:
                return e.code, payload
            raise AssertionError(f"{method} {path} -> {e.code}: {payload}")

    def wait_until(self, fn, timeout=5, message=""):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = fn()
            if last:
                return last
            time.sleep(0.03)
        raise AssertionError(f"等待超时: {message}; last={last}")

    def state(self, ref="B-001"):
        _, d = self.call("GET", f"/batches/{ref}")
        return d

    def receipt_state(self, ref, seq):
        d = self.state(ref)
        rows = {r["seq"]: r for r in d["receipts"]}
        return rows[seq], d

    def configure(self, rules, reset_calls=True):
        self.call("POST", "/admin/stub", {"rules": rules, "reset_calls": reset_calls})

    def call_count(self, idem_substr=None):
        _, d = self.call("POST", "/admin/state", {})
        calls = d["stub"]["calls"]
        if idem_substr:
            calls = [c for c in calls if idem_substr in c["idempotency_key"]]
        return len(calls)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.h = RelayServerHarness()
        self.h.call("POST", "/packages/import", {"package": package()})

    def tearDown(self):
        self.h.stop()

    # ---- 1. 执行包不可变 / 幂等 ----
    def test_package_idempotent_and_immutable(self):
        status, d = self.h.call("POST", "/packages/import", {"package": package()})
        self.assertEqual(status, 200)
        self.assertTrue(d["replay"])

        changed = package()
        changed["items"].append({"household_ref": "H-3"})
        code, err = self.h.call("POST", "/packages/import", {"package": changed},
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "package_conflict")

        _, conflicts = self.h.call("GET", "/conflicts")
        self.assertTrue(any(c["reason"] == "package_conflict"
                            for c in conflicts["conflicts"]))

    # ---- 2. 终端下载与摘要校验 ----
    def test_terminal_download_and_summary_check(self):
        _, manifest = self.h.call("GET", "/terminal/manifest")
        self.assertEqual(len(manifest["batches"]), 1)
        _, pkg = self.h.call("GET", "/terminal/packages/B-001")
        self.assertTrue(pkg["relay"]["immutable"])
        self.assertEqual(pkg["relay"]["content_hash"], manifest["batches"][0]["content_hash"])

        code, err = self.h.call("POST", "/receipts/batch",
                                batch_upload("B-001", [receipt(1, "R-1", summary="错误摘要")]),
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "summary_mismatch")

        code, err = self.h.call("POST", "/receipts/batch",
                                batch_upload("B-NOPE", [receipt(1, "R-1")]),
                                expect_error=True)
        self.assertEqual(code, 404)
        self.assertEqual(err["code"], "unknown_batch")

    # ---- 3. 乱序：缺口缓冲，补齐后连续转发 ----
    def test_out_of_order_buffering_and_continuous_forward(self):
        self.h.configure({"*": "ack"})
        _, up = self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(1, "R-1"), receipt(3, "R-3", "arrived")]))
        states = {r["seq"]: r["state"] for r in up["results"]}
        self.assertEqual(states[1], "queued")
        self.assertEqual(states[3], "buffered")

        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded", message="seq1 转发")
        _, d = self.h.receipt_state("B-001", 3)
        self.assertEqual(d["cursor"], 2)
        self.assertEqual({x["seq"] for x in d["buffered_items"]}, {3})
        self.assertEqual(d["gaps"], [2])

        # 补齐缺口 seq 2 → 2、3 连续自动转发，缓冲清空
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(2, "R-2", "failed_to_depart")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 3)[0]["state"]
                          == "forwarded", message="seq3 转发")
        self.h.wait_until(lambda: self.h.state("B-001")["cursor"] == 4,
                          message="游标到 4")
        d = self.h.state("B-001")
        self.assertEqual(d["gaps"], [])
        self.assertEqual(d["buffered_items"], [])
        self.assertEqual(d["counts"]["forwarded"], 3)

    # ---- 4a. 完全相同回执重放 ----
    def test_duplicate_receipt_replays_original_result(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        _, again = self.h.call("POST", "/receipts/batch",
                               batch_upload("B-001", [receipt(1, "R-1")]))
        self.assertEqual(again["results"][0]["status"], "duplicate_replay")
        self.assertEqual(again["results"][0]["state"], "forwarded")
        self.assertEqual(self.h.call_count("B-001:1:"), 1)  # 未重复回调

    # ---- 4b. 同序号不同内容 → 拒绝，游标不推进 ----
    def test_same_seq_different_content_rejected(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        # 序号 1 已终局，带不同内容再次上传
        bad = receipt(1, "R-1-DUP", rtype="arrived")
        code, err = self.h.call("POST", "/receipts/batch",
                                batch_upload("B-001", [bad]), expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "conflict_seq")
        d = self.h.state("B-001")
        self.assertEqual(d["cursor"], 2)  # 游标未被推回/推进

    # ---- 4c. 同回执编号不同序号不同内容 → 拒绝 ----
    def test_same_receipt_no_different_content_rejected(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(1, "R-1"), receipt(2, "R-2")]))
        self.h.wait_until(lambda: self.h.state("B-001")["cursor"] == 3)
        code, err = self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(3, "R-2")]), expect_error=True)  # R-2 已被 seq2 使用
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "conflict_receipt_no")
        self.assertEqual(self.h.state("B-001")["cursor"], 3)

    # ---- 4d. 批量上传原子性：一条冲突整批不生效 ----
    def test_batch_atomic_reject(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        # 冲突项在第二位：第一位 seq2 也必须随整批回滚
        code, err = self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(2, "R-2"),
            receipt(3, "R-1"),  # 编号冲突
        ]), expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "conflict_receipt_no")
        d = self.h.state("B-001")
        self.assertEqual(d["counts"]["forwarded"], 1)  # seq2 被回滚，没有被转发
        self.assertEqual(d["cursor"], 2)
        self.assertNotIn(2, {r["seq"] for r in d["receipts"]})
        time.sleep(0.2)  # 等几个转发周期，seq2 仍不应出现
        d = self.h.state("B-001")
        self.assertEqual(d["counts"]["forwarded"], 1)
        self.assertEqual(d["cursor"], 2)

    # ---- 5. 瞬时失败安全重试，最终恰好一次 ----
    def test_transient_failure_retried_exactly_once(self):
        self.h.configure({"B-002:1": "fail_then_ok:2"})
        self.h.call("POST", "/packages/import",
                    {"package": package("B-002")})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-002", [receipt(1, "T-1")]))
        # 前两次失败、第三次成功（均为同一幂等键）
        self.h.wait_until(
            lambda: self.h.receipt_state("B-002", 1)[0]["state"] == "forwarded",
            timeout=8, message="重试后成功")
        r, _ = self.h.receipt_state("B-002", 1)
        self.assertGreaterEqual(r["attempts"], 3)
        self.assertEqual(self.h.call_count("B-002:1:"), r["attempts"])
        self.assertEqual(self.h.state("B-002")["cursor"], 2)

    # ---- 6. 终态拒绝冻结游标，后续不得越过 ----
    def test_terminal_rejection_blocks_cursor(self):
        self.h.configure({"B-001:1": "batch_withdrawn", "*": "ack"})
        self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(1, "R-1"), receipt(2, "R-2"), receipt(3, "R-3", "arrived")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "rejected_terminal", message="终态拒绝")
        r, d = self.h.receipt_state("B-001", 1)
        self.assertEqual(r["result_code"], "batch_withdrawn")
        self.assertEqual(d["cursor"], 1)
        self.assertEqual(d["status"], "blocked_terminal")
        self.assertEqual({x["seq"] for x in d["buffered_items"]}, {2, 3})
        time.sleep(0.3)
        self.h.app.forwarder.cycle()
        d = self.h.state("B-001")
        self.assertEqual(d["cursor"], 1)  # 始终不能越过
        self.assertEqual(d["counts"]["forwarded"], 0)

        # 普通重试被拒（必须人工确认终态解除）
        code, err = self.h.call("POST", "/retries",
                                {"batch_ref": "B-001", "seq": 1}, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "terminal_requires_ack")

    # ---- 7. 确认终态解除后重投，按序继续转发 ----
    def test_terminal_ack_retry_resumes(self):
        self.h.configure({"B-001:1": "vehicle_changed", "*": "ack"})
        self.h.call("POST", "/receipts/batch", batch_upload("B-001", [
            receipt(1, "R-1"), receipt(2, "R-2")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "rejected_terminal")
        # 主系统改回车辆 → 改桩规则为 ack，人工确认重投
        self.h.configure({"*": "ack"}, reset_calls=False)
        self.h.call("POST", "/batches/B-001/receipts/1/ack-terminal",
                    {"note": "车辆 V-07 已恢复，编排系统确认可重投"})
        self.h.wait_until(lambda: self.h.state("B-001")["cursor"] == 3,
                          message="恢复后连续转发到 3")
        d = self.h.state("B-001")
        self.assertEqual(d["counts"]["forwarded"], 2)
        self.assertIsNone(d["blocker"])

    def test_destination_changed_terminal(self):
        self.h.configure({"B-001:1": "destination_changed", "*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "rejected_terminal")
        _, aud = self.h.call("GET", "/audit")
        self.assertTrue(any(e["kind"] == "forward_terminal_reject"
                            and "destination_changed" in e["detail"]
                            for e in aud["events"]))

    # ---- 8. 进程重启恢复 ----
    def test_restart_recovery_no_duplicate_callback(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        db_path = self.h.db_path
        tmp_dir = self.h.tmp
        adapter = self.h.adapter  # 复用同一桩（保留调用计数与规则）
        before_calls = self.h.call_count("B-001:1:")
        self.h.stop(keep_data=True)
        self.h.tmp = None  # 临时目录所有权移交给 h2，由它结束时清理

        # 模拟重启：新进程打开同一 DB 文件（启动恢复 + 队列继续）
        h2 = RelayServerHarness(db_path=db_path, adapter=adapter)
        h2.tmp = tmp_dir
        h2.configure({"*": "ack"}, reset_calls=False)
        try:
            self.assertEqual(h2.app.startup["recovered_leases"], 0)
            # 再传 seq 2，验证队列在重启后的实例上继续工作
            h2.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(2, "R-2")]))
            h2.wait_until(lambda: h2.receipt_state("B-001", 2)[0]["state"]
                          == "forwarded", message="重启后继续转发")
            self.assertEqual(h2.state("B-001")["cursor"], 3)
            # seq1 重启前后都没有再次回调
            self.assertEqual(h2.call_count("B-001:1:"), before_calls)
        finally:
            h2.stop()

    def test_restart_with_dispatching_lease_reclaimed(self):
        # 直接落一条 queued 回执后关停进程，再模拟"崩溃在 dispatching 且租约过期"
        self.h.configure({"*": "ack"})
        with self.h.app.db.lock:
            core.ingest_receipt(self.h.app.db,
                                {**receipt(1, "R-1"), "batch_ref": "B-001"})
        db_path = self.h.db_path
        tmp_dir = self.h.tmp
        adapter = adapters.StubCallbackAdapter()
        self.h.stop(keep_data=True)
        self.h.tmp = None
        # 模拟进程崩溃：行停留在 dispatching、租约已过期
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE receipts SET state='dispatching',lease_until=?,"
                     "attempts=1 WHERE batch_ref='B-001' AND seq=1",
                     (time.time() - 5,))
        conn.commit()
        conn.close()

        h2 = RelayServerHarness(db_path=db_path, adapter=adapter)
        h2.tmp = tmp_dir
        try:
            self.assertEqual(h2.app.startup["recovered_leases"], 1)
            h2.configure({"*": "ack"})
            h2.wait_until(lambda: h2.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded", message="回收后转发成功")
        finally:
            h2.stop()

    # ---- 9. 手动立即重试 ----
    def test_manual_retry_short_circuits_backoff(self):
        self.h.configure({"B-001:1": "http_503"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        # 等到至少完成一次失败派发并稳定在 retry_wait
        time.sleep(0.1)
        state_now, _ = self.h.receipt_state("B-001", 1)
        self.assertEqual(state_now["state"], "retry_wait")
        self.h.configure({"*": "ack"}, reset_calls=False)
        _, d = self.h.call("POST", "/retries",
                           {"batch_ref": "B-001", "seq": 1, "note": "网络恢复"})
        self.assertEqual(d["retried"], [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")

    def test_audit_records_milestones(self):
        self.h.configure({"*": "ack"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        _, aud = self.h.call("GET", "/audit")
        kinds = {e["kind"] for e in aud["events"]}
        for need in ("relay_started", "package_imported", "receipt_received",
                     "dispatch", "forward_ok"):
            self.assertIn(need, kinds)

    # ---- 10. 并发上传不产生重复/脏状态（全局锁串行化）----
    def test_concurrent_uploads_are_serialized(self):
        self.h.configure({"*": "ack"})
        refs = ["B-C1", "B-C2", "B-C3", "B-C4"]
        for ref in refs:
            self.h.call("POST", "/packages/import",
                        {"package": package(ref)})

        errors, results = [], []

        def worker(ref, base):
            try:
                for seq in range(1, 4):
                    code, d = self.h.call(
                        "POST", "/receipts/batch",
                        batch_upload(ref, [receipt(seq, f"{ref}-R{seq}")]),
                        expect_error=True)
                    results.append((ref, seq, code, d.get("status")))
            except AssertionError as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(ref, 0)) for ref in refs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(errors, errors)
        for ref in refs:
            self.h.wait_until(lambda r=ref: self.h.state(r)["cursor"] == 4,
                              message=f"{ref} 全部转发")
            d = self.h.state(ref)
            self.assertEqual(len(d["receipts"]), 3)  # 无重复行
            self.assertEqual(d["counts"]["forwarded"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
