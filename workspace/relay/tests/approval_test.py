"""岗位权限与双人复核端到端测试。"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, HERE)

import adapters  # noqa: E402
import approvals  # noqa: E402
import server as srv  # noqa: E402
from test_relay import RelayServerHarness, SUMMARY, batch_upload, package, receipt  # noqa: E402


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.h = RelayServerHarness()
        with self.h.app.db.lock:
            import core
            core.import_package(self.h.app.db, package(), actor="test-setup")
        self.h.configure({"*": "ack"})

    def tearDown(self):
        self.h.stop()

    def raw(self, method, path, body=None, actor=None, expect_error=False):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if actor:
            headers["X-Relay-User"] = actor
        req = urllib.request.Request(self.h.base + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            payload = json.loads(e.read().decode())
            if expect_error:
                return e.code, payload
            raise AssertionError(f"{method} {path} -> {e.code}: {payload}")

    def request_and_approve(self, path, body, applicant="dispatcher",
                            approver=None, action="dispatch",
                            expect_error=False):
        role = "archive_review" if action == "archive_review" else "dispatch"
        approver = approver or ("reviewer" if action == "archive_review" else "admin")
        code, created = self.raw("POST", path, body, applicant, expect_error)
        if code >= 400:
            return code, created
        if not created.get("approval_required"):
            return code, created
        rid = created["request"]["id"]
        return self.raw("POST", f"/approvals/{rid}/approve",
                        {"note": "第二人复核"}, approver, expect_error)

    def test_requires_login_and_roles(self):
        code, err = self.raw("POST", "/packages/import",
                             {"package": package("B-AUTH")}, expect_error=True)
        self.assertEqual((code, err["code"]), (401, "unauthenticated"))
        code, err = self.raw("POST", "/packages/import",
                             {"package": package("B-AUTH")}, "observer", True)
        self.assertEqual((code, err["code"]), (403, "forbidden_role"))
        code, created = self.raw("POST", "/packages/import",
                                 {"package": package("B-AUTH")}, "dispatcher")
        self.assertEqual((code, created["approval_required"]), (202, True))
        code, err = self.raw("POST", f"/approvals/{created['request']['id']}/approve",
                             {"note": "x"}, "dispatcher", True)
        self.assertEqual((code, err["code"]), (403, "self_approval"))
        code, err = self.raw("POST", f"/approvals/{created['request']['id']}/approve",
                             {"note": "x"}, "observer", True)
        self.assertEqual((code, err["code"]), (403, "unauthorized_role"))
        code, ok = self.raw("POST", f"/approvals/{created['request']['id']}/approve",
                            {"note": "x"}, "deputy")
        self.assertEqual(code, 200)
        self.assertTrue(ok["effective"])

    def test_duplicate_request_and_concurrent_approval_are_idempotent(self):
        code, c1 = self.raw("POST", "/packages/import",
                            {"package": package("B-IDEM")}, "dispatcher")
        rid = c1["request"]["id"]
        code, c2 = self.raw("POST", "/packages/import",
                            {"package": package("B-IDEM")}, "dispatcher")
        self.assertTrue(c2["replay"])
        self.assertEqual(c2["request"]["id"], rid)

        results = []

        def approve(username):
            try:
                results.append(self.raw("POST", f"/approvals/{rid}/approve",
                                        {"note": username}, username))
            except AssertionError:
                pass

        threads = [threading.Thread(target=approve, args=(u,))
                   for u in ("admin", "deputy")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        effective = [p for _, p in results if p.get("effective")]
        self.assertEqual(len(effective), 1)
        code, again = self.raw("POST", f"/approvals/{rid}/approve",
                               {"note": "重复批准"}, "admin", True)
        self.assertEqual(code, 409)
        self.assertEqual(again["code"], "replay_approved")
        _, detail = self.raw("GET", f"/approvals/{rid}", actor="observer")
        self.assertEqual(detail["request"]["state"], "approved")
        self.assertGreaterEqual(len(detail["request"]["decisions"]), 3)
        self.assertEqual(sum(d["effective"] for d in detail["request"]["decisions"]), 1)

    def test_role_change_disable_and_resource_change_invalidate_pending(self):
        _, created = self.raw("POST", "/packages/import",
                              {"package": package("B-ROLE")}, "dispatcher")
        rid = created["request"]["id"]
        self.raw("POST", "/accounts",
                 {"username": "dispatcher", "roles": ["observe"]}, "admin")
        _, d = self.raw("GET", f"/approvals/{rid}", actor="admin")
        self.assertEqual(d["request"]["state"], "invalid")
        self.assertEqual(d["request"]["invalid_reason"], "role_changed")
        code, err = self.raw("POST", f"/approvals/{rid}/approve", {},
                             "reviewer", True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "replay_invalid")
        # 恢复岗位后旧事项仍不可批准
        self.raw("POST", "/accounts",
                 {"username": "dispatcher", "roles": ["observe", "dispatch"]}, "admin")
        _, d = self.raw("GET", f"/approvals/{rid}", actor="admin")
        self.assertEqual(d["request"]["state"], "invalid")

        # 资源版本变化：先对 retry_wait 建待办，后台/状态改变后批准必须拒绝。
        self.h.configure({"B-001:1": "http_503"})
        self.h.call("POST", "/receipts/batch",
                    batch_upload("B-001", [receipt(1, "R-1")]))
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        self.h.app.forwarder.stop()
        self.h.configure({"B-001:1": "ack", "*": "ack"}, reset_calls=False)
        _, retry_req = self.raw("POST", "/retries",
                                {"batch_ref": "B-001", "seq": 1}, "dispatcher")
        # 另一个动作先把状态推进到终态成功，原待办资源版本随即失效。
        with self.h.app.db.lock:
            import core
            core.manual_retry(self.h.app.db, "B-001", seq=1, actor="system")
        self.h.app.forwarder._stop = False
        self.h.app.forwarder.cycle()
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        code, err = self.raw("POST",
                             f"/approvals/{retry_req['request']['id']}/approve",
                             {}, "reviewer", True)
        self.assertEqual((code, err["code"]), (409, "replay_invalid"))
        _, d = self.raw("GET", f"/approvals/{retry_req['request']['id']}",
                        actor="admin")
        self.assertEqual(d["request"]["invalid_reason"], "resource_changed")

    def test_timeout_invalidates_and_effective_action_is_not_rolled_back(self):
        import core
        old_ttl = approvals.ttl_seconds
        approvals.ttl_seconds = lambda: -1
        try:
            _, created = self.raw("POST", "/packages/import",
                                  {"package": package("B-TTL")}, "dispatcher")
            rid = created["request"]["id"]
            _, d = self.raw("GET", f"/approvals/{rid}", actor="admin")
            self.assertEqual(d["request"]["state"], "invalid")
            self.assertEqual(d["request"]["invalid_reason"], "expired")
        finally:
            approvals.ttl_seconds = old_ttl

        # 已生效动作不会因后续停用/超时回滚。
        _, done = self.request_and_approve(
            "/packages/import", {"package": package("B-DONE")},
            applicant="dispatcher", approver="admin")
        self.assertTrue(done.get("effective"))
        self.raw("POST", "/accounts",
                 {"username": "dispatcher", "active": False}, "admin")
        _, detail = self.raw("GET", "/batches/B-DONE", actor="admin")
        self.assertEqual(detail["batch_ref"], "B-DONE")

    def test_views_show_mine_invalid_and_decision_log(self):
        _, c = self.raw("POST", "/packages/import",
                        {"package": package("B-VIEW")}, "dispatcher")
        rid = c["request"]["id"]
        self.raw("POST", f"/approvals/{rid}/reject",
                 {"note": "不同意"}, "admin")
        _, mine = self.raw("GET", "/approvals/mine", actor="dispatcher")
        self.assertIn(rid, [x["id"] for x in mine["requests"]])
        _, invalid = self.raw("GET", "/approvals/invalid", actor="observer")
        self.assertIn("rejected", [x["state"] for x in invalid["requests"]])
        _, log = self.raw("GET", "/approval-decisions", actor="observer")
        self.assertTrue(any(d["request_id"] == rid and d["decision"] == "reject"
                            for d in log["decisions"]))

    def test_restart_restores_approval_state(self):
        _, c = self.raw("POST", "/packages/import",
                        {"package": package("B-RESTART")}, "dispatcher")
        rid = c["request"]["id"]
        db_path, tmp_dir, adapter = self.h.db_path, self.h.tmp, self.h.adapter
        self.h.stop(keep_data=True)
        self.h.tmp = None
        h2 = RelayServerHarness(db_path=db_path, adapter=adapter)
        h2.tmp = tmp_dir
        try:
            _, d = h2.call("GET", f"/approvals/{rid}", actor="admin")
            self.assertEqual(d["request"]["state"], "pending")
            _, ok = h2.call("POST", f"/approvals/{rid}/approve",
                            {"note": "重启后复核"}, actor="admin")
            self.assertTrue(ok["effective"])
            _, pkg = h2.call("GET", "/terminal/packages/B-RESTART")
            self.assertEqual(pkg["batch_ref"], "B-RESTART")
        finally:
            h2.stop()

    def test_approved_action_still_runs_original_validation(self):
        # 批次不存在：创建时即拒绝，无法绕过业务校验。
        code, err = self.request_and_approve(
            "/batches/B-NOPE/seal", {}, applicant="admin",
            approver="reviewer", action="archive_review", expect_error=True)
        self.assertEqual(code, 404)
        self.assertEqual(err["code"], "not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
