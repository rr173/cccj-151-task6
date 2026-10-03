"""批次封存与只读归档 —— 端到端测试（Python 标准库 unittest）。

覆盖：
  1. 热库占用预估与封存资格（缺口/缓冲/待重试/终态阻塞 → 不可封存并列出原因）
  2. 全部回执结束后封存成功：内容清单 + 校验摘要、热库明细迁入只读归档、
     空间回收、原审计时间线随归档保留
  3. 同一批次重复封存幂等返回第一次结果
  4. 封存前完整性校验失败（热库被篡改）→ 拒绝且不产生半迁移
  5. 封存后新回执 409 batch_archived；完全相同的历史回执按归档重放；
     归档记录永不回到转发队列（适配器零新增调用）
  6. 归档校验并恢复只读详情；归档被篡改 → 校验失败拒绝恢复
  7. 归档下载文档可离线独立重算摘要
  8. 进程重启后归档状态完整恢复；封存中途失败整体回滚、可再次封存

运行：python3 relay/tests/archive_test.py
"""
import hashlib
import json
import os
import sqlite3
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, HERE)

import archive  # noqa: E402
from test_relay import (  # noqa: E402
    RelayServerHarness, SUMMARY, batch_upload, package, receipt)


def canonical_sha256(obj):
    return hashlib.sha256(json.dumps(
        obj, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.h = RelayServerHarness()
        self.h.call("POST", "/packages/import", {"package": package()})
        self.h.configure({"*": "ack"})

    def tearDown(self):
        self.h.stop()

    # ---- 工具 ----
    def forward_all(self, ref="B-001", n=3):
        self.h.call("POST", "/receipts/batch",
                    batch_upload(ref, [receipt(i, f"R-{i}") for i in range(1, n + 1)]))
        self.h.wait_until(lambda: self.h.state(ref)["cursor"] == n + 1,
                          message=f"{ref} 全部转发完成")

    def batch_audit_count(self, ref="B-001"):
        _, aud = self.h.call("GET", "/audit")
        return len([e for e in aud["events"] if e["batch_ref"] == ref])

    # ---- 1. 预估与资格 ----
    def test_estimate_reports_hot_usage_and_blockers(self):
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1"),
                                           receipt(3, "R-3", "arrived")]))
        _, s = self.h.call("GET", "/admin/storage")
        self.assertEqual(len(s["batches"]), 1)
        b = s["batches"][0]
        self.assertFalse(b["sealable"])
        codes = {r["code"] for r in b["reasons"]}
        self.assertIn("has_gaps", codes)
        self.assertGreater(b["hot_bytes"], 0)
        self.assertEqual(b["receipt_count"], 2)
        self.assertGreater(b["audit_count"], 0)
        self.assertEqual(s["totals"]["archived"], 0)

    def test_seal_rejected_while_in_progress(self):
        # 乱序 1,3：seq1 转发中/已转发，seq3 缓冲，缺口 2 → 拒绝并列出原因
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1"),
                                           receipt(3, "R-3", "arrived")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        code, err = self.h.call("POST", "/batches/B-001/seal", {},
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "batch_not_sealable")
        codes = {r["code"] for r in err["reasons"]}
        self.assertIn("has_gaps", codes)
        self.assertIn("pending_buffered", codes)
        # 热库数据未受影响
        d = self.h.state("B-001")
        self.assertEqual(len(d["receipts"]), 2)
        self.assertFalse(d["archived"])

    def test_seal_rejected_with_retry_wait_and_terminal(self):
        self.h.configure({"B-001:1": "http_503"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        code, err = self.h.call("POST", "/batches/B-001/seal", {},
                                expect_error=True)
        self.assertEqual(err["code"], "batch_not_sealable")
        # 退避中的回执在查看瞬间可能正被重新派发，两种"未结束"都成立
        self.assertTrue(any(r["code"] in ("pending_retry_wait", "pending_dispatching")
                            for r in err["reasons"]))
        # 终态阻塞同样不可封存
        self.h.configure({"B-001:1": "batch_withdrawn"}, reset_calls=False)
        self.h.call("POST", "/retries", {"batch_ref": "B-001", "seq": 1})
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "rejected_terminal")
        code, err = self.h.call("POST", "/batches/B-001/seal", {},
                                expect_error=True)
        self.assertEqual(err["code"], "batch_not_sealable")
        self.assertTrue(any(r["code"] == "pending_rejected_terminal"
                            for r in err["reasons"]))

    # ---- 2. 封存成功：清单/摘要/迁移/回收/审计时间线 ----
    def test_seal_success_moves_hot_data_to_archive(self):
        self.forward_all()
        audit_before = self.batch_audit_count()
        self.assertGreater(audit_before, 0)

        status, r = self.h.call("POST", "/batches/B-001/seal",
                                {"note": "月底空间回收"})
        self.assertEqual(status, 200)
        self.assertFalse(r["replay"])
        self.assertTrue(r["sealed"])
        self.assertEqual(r["receipt_count"], 3)
        self.assertGreater(r["freed_bytes"], 0)
        self.assertEqual(len(r["digest"]), 64)
        m = r["manifest"]
        self.assertEqual(m["format"], "relay-archive/v1")
        self.assertEqual(m["receipt_count"], 3)
        self.assertEqual([e["seq"] for e in m["receipts"]], [1, 2, 3])
        self.assertTrue(all(e["content_hash"] for e in m["receipts"]))
        self.assertEqual(m["cursor_at_seal"], 4)
        self.assertEqual(m["note"], "月底空间回收")

        # 热库空间已回收
        _, s = self.h.call("GET", "/admin/storage")
        b = s["batches"][0]
        self.assertTrue(b["archived"])
        self.assertEqual(b["hot_bytes"], 0)
        self.assertEqual(s["totals"]["archived"], 1)
        self.assertGreater(s["totals"]["archive_bytes"], 0)

        # 批次详情来自只读归档（含原审计时间线）
        _, d = self.h.call("GET", "/batches/B-001")
        self.assertTrue(d["archived"])
        self.assertTrue(d["readonly"])
        self.assertEqual(d["status"], "archived")
        self.assertEqual(len(d["receipts"]), 3)
        self.assertTrue(all(x["state"] == "forwarded" for x in d["receipts"]))
        self.assertEqual(len(d["audit"]), audit_before)
        kinds = {e["kind"] for e in d["audit"]}
        self.assertIn("receipt_received", kinds)
        self.assertIn("forward_ok", kinds)
        self.assertTrue(d["verification"]["ok"])

        # 热库审计流中该批次只剩封存事件本身
        _, aud = self.h.call("GET", "/audit")
        batch_events = [e for e in aud["events"] if e["batch_ref"] == "B-001"]
        self.assertEqual({e["kind"] for e in batch_events}, {"batch_sealed"})

        # 归档列表
        _, a = self.h.call("GET", "/archives")
        self.assertEqual(len(a["archives"]), 1)
        self.assertEqual(a["archives"][0]["digest"], r["digest"])

    # ---- 3. 幂等：重复封存返回第一次结果 ----
    def test_seal_replay_returns_first_result(self):
        self.forward_all()
        _, r1 = self.h.call("POST", "/batches/B-001/seal", {"note": "第一次"})
        _, r2 = self.h.call("POST", "/batches/B-001/seal", {"note": "第二次"})
        self.assertTrue(r2["replay"])
        self.assertEqual(r2["digest"], r1["digest"])
        self.assertEqual(r2["sealed_at"], r1["sealed_at"])
        self.assertEqual(r2["manifest"]["note"], "第一次")
        _, a = self.h.call("GET", "/archives")
        self.assertEqual(len(a["archives"]), 1)

    # ---- 4. 校验失败拒绝，不留半迁移 ----
    def test_seal_checksum_mismatch_rejected(self):
        self.forward_all()
        # 直接篡改热库回执原文（不更新入账哈希）
        conn = sqlite3.connect(self.h.db_path)
        conn.execute(
            "UPDATE receipts SET payload=? WHERE batch_ref='B-001' AND seq=2",
            (json.dumps({"seq": 2, "receipt_no": "R-2", "type": "departed",
                         "batch_summary": SUMMARY, "at": "被篡改"},
                        ensure_ascii=False),))
        conn.commit()
        conn.close()
        code, err = self.h.call("POST", "/batches/B-001/seal", {},
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "checksum_mismatch")
        self.assertEqual(err["errors"][0]["seq"], 2)
        # 无半迁移：热库明细完整、无归档、未置封存标记
        _, d = self.h.call("GET", "/batches/B-001")
        self.assertFalse(d["archived"])
        self.assertEqual(len(d["receipts"]), 3)
        _, a = self.h.call("GET", "/archives")
        self.assertEqual(a["archives"], [])

    # ---- 5. 归档记录永不回到转发队列 ----
    def test_archived_batch_never_reenters_queue(self):
        self.forward_all()
        calls_before = self.h.call_count()
        self.h.call("POST", "/batches/B-001/seal", {})

        # 完全相同的历史回执 → 按归档重放原结果
        _, again = self.h.call("POST", "/receipts/batch",
                               batch_upload("B-001", [receipt(1, "R-1")]))
        self.assertEqual(again["results"][0]["status"], "duplicate_replay")
        self.assertEqual(again["results"][0]["state"], "archived")

        # 新回执 → 409 batch_archived
        code, err = self.h.call("POST", "/receipts/batch",
                                batch_upload("B-001", [receipt(4, "R-4")]),
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "batch_archived")
        # 同序号不同内容 → 同样 batch_archived（热库无行可比对，归档优先）
        code, err = self.h.call("POST", "/receipts",
                                {**receipt(2, "R-2-DIFF"), "batch_ref": "B-001"},
                                expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "batch_archived")
        # 手动重试 / 终态确认重投均被拒
        code, err = self.h.call("POST", "/retries", {"batch_ref": "B-001"},
                                expect_error=True)
        self.assertEqual(err["code"], "batch_archived")
        code, err = self.h.call(
            "POST", "/batches/B-001/receipts/1/ack-terminal", {},
            expect_error=True)
        self.assertEqual(err["code"], "batch_archived")

        # 队列为空；转发器空转若干周期后适配器零新增调用
        _, q = self.h.call("GET", "/queue")
        self.assertEqual(q["total"], 0)
        for _ in range(3):
            self.h.app.forwarder.cycle()
        self.assertEqual(self.h.call_count(), calls_before)

    # ---- 6. 校验并恢复只读详情；篡改归档被拒绝 ----
    def test_archive_verify_and_readonly_restore(self):
        self.forward_all()
        _, r = self.h.call("POST", "/batches/B-001/seal", {})
        _, d = self.h.call("GET", "/archives/B-001")
        self.assertTrue(d["readonly"])
        self.assertTrue(d["verification"]["ok"])
        self.assertEqual(d["verification"]["digest_actual"], r["digest"])
        self.assertEqual(len(d["receipts"]), 3)
        self.assertEqual(d["receipts"][0]["receipt_no"], "R-1")
        self.assertEqual(d["receipts"][0]["payload"]["receipt_no"], "R-1")
        self.assertEqual(d["package"]["batch_ref"], "B-001")
        self.assertTrue(d["audit"])
        # 未封存批次无归档
        self.h.call("POST", "/packages/import",
                    {"package": package("B-009")})
        code, err = self.h.call("GET", "/archives/B-009", expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "not_archived")
        code, err = self.h.call("GET", "/archives/B-NOPE", expect_error=True)
        self.assertEqual(code, 404)

    def test_archive_tamper_detected_and_rejected(self):
        self.forward_all()
        self.h.call("POST", "/batches/B-001/seal", {})
        # 篡改归档正文（不更新摘要）
        conn = sqlite3.connect(self.h.db_path)
        doc = json.loads(conn.execute(
            "SELECT payload FROM archives WHERE batch_ref='B-001'").fetchone()[0])
        doc["receipts"][0]["receipt_no"] = "R-TAMPERED"
        conn.execute("UPDATE archives SET payload=? WHERE batch_ref='B-001'",
                     (json.dumps(doc, ensure_ascii=False),))
        conn.commit()
        conn.close()
        code, err = self.h.call("GET", "/archives/B-001", expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "archive_corrupt")
        self.assertFalse(err["ok"])
        # 批次详情同样拒绝恢复
        code, err = self.h.call("GET", "/batches/B-001", expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "archive_corrupt")
        # 失败留痕
        _, aud = self.h.call("GET", "/audit")
        self.assertTrue(any(e["kind"] == "archive_verify_failed"
                            for e in aud["events"]))

    # ---- 7. 下载文档可离线校验 ----
    def test_archive_download_document_verifiable(self):
        self.forward_all()
        _, r = self.h.call("POST", "/batches/B-001/seal", {})
        _, doc = self.h.call("GET", "/archives/B-001/download")
        self.assertEqual(doc["format"], "relay-archive/v1")
        self.assertEqual(doc["digest"], r["digest"])
        self.assertEqual(doc["manifest"]["receipt_count"], 3)
        # 去掉附加字段后重算 canonical sha256 必须等于 digest
        payload = {k: doc[k]
                   for k in ("format", "manifest", "package", "receipts", "audit")}
        self.assertEqual(canonical_sha256(payload), r["digest"])

    # ---- 8. 重启恢复与中途失败回滚 ----
    def test_restart_after_seal_restores_archive_state(self):
        self.forward_all()
        _, r1 = self.h.call("POST", "/batches/B-001/seal", {})
        db_path, tmp_dir, adapter = self.h.db_path, self.h.tmp, self.h.adapter
        self.h.stop(keep_data=True)
        self.h.tmp = None

        h2 = RelayServerHarness(db_path=db_path, adapter=adapter)
        h2.tmp = tmp_dir
        try:
            self.assertEqual(h2.app.startup["archive_repairs"], 0)
            _, lst = h2.call("GET", "/batches")
            self.assertEqual(lst["batches"][0]["status"], "archived")
            _, d = h2.call("GET", "/batches/B-001")
            self.assertTrue(d["archived"])
            self.assertEqual(len(d["receipts"]), 3)
            self.assertTrue(d["audit"])
            # 重复封存仍返回第一次结果
            _, r2 = h2.call("POST", "/batches/B-001/seal", {})
            self.assertTrue(r2["replay"])
            self.assertEqual(r2["digest"], r1["digest"])
            # 新回执仍被拒，归档记录不回到队列
            code, err = h2.call("POST", "/receipts/batch",
                                batch_upload("B-001", [receipt(4, "R-4")]),
                                expect_error=True)
            self.assertEqual(err["code"], "batch_archived")
            _, q = h2.call("GET", "/queue")
            self.assertEqual(q["total"], 0)
        finally:
            h2.stop()

    def test_mid_seal_failure_rolls_back(self):
        self.forward_all()
        audit_before = self.batch_audit_count()
        # 模拟"写入后、提交前"崩溃：audit 在 DELETE/INSERT 之后、commit 之前被调用
        orig_audit = self.h.app.db.audit

        def boom(*a, **k):
            raise RuntimeError("模拟封存中途崩溃")

        self.h.app.db.audit = boom
        try:
            code, err = self.h.call("POST", "/batches/B-001/seal", {},
                                    expect_error=True)
            self.assertEqual(code, 500)
        finally:
            self.h.app.db.audit = orig_audit
        # 不留半迁移状态：热库明细完整、审计行未丢、无归档行、未置封存标记
        _, d = self.h.call("GET", "/batches/B-001")
        self.assertFalse(d["archived"])
        self.assertEqual(len(d["receipts"]), 3)
        self.assertEqual(self.batch_audit_count(), audit_before)
        _, a = self.h.call("GET", "/archives")
        self.assertEqual(a["archives"], [])
        _, s = self.h.call("GET", "/admin/storage")
        self.assertGreater(s["batches"][0]["hot_bytes"], 0)
        # 故障排除后可再次成功封存
        _, r = self.h.call("POST", "/batches/B-001/seal", {})
        self.assertTrue(r["sealed"])
        self.assertFalse(r["replay"])
        _, d = self.h.call("GET", "/batches/B-001")
        self.assertTrue(d["archived"])

    def test_seal_unknown_batch_404(self):
        code, err = self.h.call("POST", "/batches/B-NOPE/seal", {},
                                expect_error=True)
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
