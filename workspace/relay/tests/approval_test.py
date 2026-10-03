"""中继值班操作 —— 岗位权限与双人复核端到端测试（Python 标准库 unittest）。

覆盖：
  1. 三类岗位权限：观察只读、调度/归档审查分管敏感指令，越权 403
  2. 敏感指令直调被拒（approval_required），必须走申请→另一账号批准
  3. 申请人不能自批；批准人需具备对应岗位
  4. 带资源版本与请求指纹；重复申请/重复批准幂等（只执行一次）
  5. 并发批准只有一次生效
  6. 目标资源状态变化 → 未决事项立即失效，批准返回 409
  7. 账号停用 / 角色调整 → 未决事项立即失效；已生效动作不回滚
  8. 超时失效（RELAY_APPROVAL_TTL）
  9. 批准不绕过原业务校验（不可封存/终态未解除 → execute_failed）
 10. 服务重启后待办/失效/决策记录完整恢复
 11. 本人待办、已失效事项、完整决策记录接口
 12. 四类敏感指令（导入/重试/终态解除/封存）全链路

运行：python3 relay/tests/approval_test.py
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

# 默认演示账号
OBS = "zhang_obs"      # 仅观察
DISP1 = "li_disp"      # 调度（申请人）
DISP2 = "wang_disp"    # 调度（另一批准人）
ARCH1 = "zhao_arch"    # 归档审查
ARCH2 = "sun_arch"     # 归档审查（另一批准人）

SUMMARY = "波次1 · V-07 · G3 → S1（4户9人）"


def package(ref="B-001", summary=SUMMARY):
    return {"batch_ref": ref, "package_id": "PKG-1", "batch_summary": summary,
            "seq_base": 1, "issued_at": "2026-10-02T07:30:00Z",
            "vehicle": {"code": "V-07", "seats": 11},
            "destination": {"code": "S1", "name": "城北体育馆"},
            "items": [{"household_ref": "H-1"}, {"household_ref": "H-2"}]}


def receipt(seq, no, rtype="departed", summary=SUMMARY):
    return {"seq": seq, "receipt_no": no, "type": rtype,
            "batch_summary": summary, "at": "2026-10-02T08:00:00Z"}


class ApprovalHarness:
    def __init__(self, db_path=None, adapter=None, ttl=None):
        self.tmp = None
        if db_path is None:
            self.tmp = tempfile.mkdtemp(prefix="relay-appr-")
            db_path = os.path.join(self.tmp, "relay.db")
        self.db_path = db_path
        self.adapter = adapter or adapters.StubCallbackAdapter()
        # 每个实例显式设置 TTL（0=用默认），避免上一用例的 RELAY_APPROVAL_TTL 残留
        os.environ["RELAY_APPROVAL_TTL"] = str(ttl if ttl is not None else 900)
        kw = dict(db_path=db_path, adapter=self.adapter,
                  enforce_approvals=True, interval=0.02,
                  lease_seconds=30, backoff_base=0.03, backoff_cap=0.5)
        self.app = srv.RelayApp(**kw)
        srv.APP = self.app
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

    def call(self, method, path, body=None, user=None, expect_error=False,
             headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if data else {}
        if user:
            hdrs["X-Relay-User"] = user
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers=hdrs)
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

    def state(self, ref="B-001", user=OBS):
        _, d = self.call("GET", f"/batches/{ref}", user=user)
        return d

    def receipt_state(self, ref, seq, user=OBS):
        d = self.state(ref, user)
        return {r["seq"]: r for r in d["receipts"]}[seq], d

    def configure(self, rules, reset_calls=True):
        self.call("POST", "/admin/stub", {"rules": rules, "reset_calls": reset_calls},
                  user=DISP1)

    def call_count(self, idem_substr=None):
        _, d = self.call("POST", "/admin/state", {}, user=DISP1)
        calls = d["stub"]["calls"]
        if idem_substr:
            calls = [c for c in calls if idem_substr in c["idempotency_key"]]
        return len(calls)

    # ---- 申请 → 另一账号批准 ----
    def apply(self, action, requester, params):
        return self.call("POST", "/approvals", {"action": action, **params},
                         user=requester)

    def decide(self, ap_id, approver, decision="approve", note="测试复核",
               expect_error=False):
        return self.call("POST", f"/approvals/{ap_id}/decide",
                         {"decision": decision, "note": note},
                         user=approver, expect_error=expect_error)

    def pair(self, action, requester, approver, params, expect_error=False):
        _, d = self.apply(action, requester, params)
        return self.decide(d["id"], approver, expect_error=expect_error)

    def get_ap(self, ap_id, user=OBS):
        _, d = self.call("GET", f"/approvals/{ap_id}", user=user)
        return d


def import_pkg(h, ref="B-001", requester=DISP1, approver=DISP2):
    return h.pair("package_import", requester, approver,
                  {"package": package(ref)})


def forward_receipts(h, ref, seqnos, rules=None, summary=SUMMARY):
    if rules is not None:
        h.configure(rules)
    receipts = [receipt(s, f"R-{s}", "arrived" if s > 1 else "departed",
                        summary) for s in seqnos]
    _, up = h.call("POST", "/receipts/batch",
                   {"batch_ref": ref, "batch_summary": summary, "receipts": receipts},
                   user=OBS)
    return up


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.h = ApprovalHarness()

    def tearDown(self):
        self.h.stop()

    # ---- 1. 身份与岗位权限 ----
    def test_01_identity_and_roles(self):
        code, err = self.h.call("GET", "/batches", expect_error=True)
        self.assertEqual(code, 401)
        self.assertEqual(err["code"], "unauthenticated")
        code, err = self.h.call("GET", "/batches", user="nobody",
                                expect_error=True)
        self.assertEqual(code, 401)
        self.assertEqual(err["code"], "unknown_account")
        # 观察员可读
        _, d = self.h.call("GET", "/batches", user=OBS)
        self.assertEqual(d["batches"], [])
        _, me = self.h.call("GET", "/me", user=OBS)
        self.assertEqual(me["role_list"], ["observe"])

    def test_02_observe_cannot_request_sensitive(self):
        # 观察员尝试导入申请 → 403 forbidden_role
        code, err = self.h.call(
            "POST", "/approvals",
            {"action": "package_import", "package": package()},
            user=OBS, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "forbidden_role")

    def test_03_direct_sensitive_call_requires_approval(self):
        # 即使具备调度权限，直调导入也被拒，必须走复核
        code, err = self.h.call("POST", "/packages/import",
                                {"package": package()}, user=DISP1,
                                expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "approval_required")
        self.assertEqual(err["action"], "package_import")

    # ---- 2. 正常双人复核链路：导入 ----
    def test_04_two_person_import_works(self):
        status, d = import_pkg(self.h)
        self.assertEqual(status, 200)
        self.assertEqual(d["status"], "executed")
        self.assertTrue(d["execution_result_obj"])
        self.assertEqual(d["decided_by"], DISP2)
        self.assertFalse(d["execution_result_obj"]["replay"])
        # 执行包确实落库（观察员可见）
        self.assertEqual(len(self.h.state()["payload"]["items"]), 2)
        _, dec = self.h.call("GET", "/approval-decisions", user=OBS)
        kinds = {x["kind"] for x in dec["decisions"]}
        for need in ("requested", "approved", "executed"):
            self.assertIn(need, kinds)

    # ---- 3. 申请人不能自批；批准人权限不足 ----
    def test_05_self_approval_forbidden(self):
        _, d = self.h.apply("package_import", DISP1, {"package": package()})
        code, err = self.h.decide(d["id"], DISP1, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "self_approval_forbidden")
        # 观察员也不能批
        code, err = self.h.decide(d["id"], OBS, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "forbidden_role")
        # 归档审查不能批调度类
        code, err = self.h.decide(d["id"], ARCH1, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "forbidden_role")
        # 仍可由另一调度批准
        status, ok = self.h.decide(d["id"], DISP2)
        self.assertEqual(ok["status"], "executed")

    # ---- 4. 幂等：重复申请返回同一事项；重复批准只执行一次 ----
    def test_06_duplicate_request_and_approval_idempotent(self):
        _, d1 = self.h.apply("package_import", DISP1, {"package": package()})
        _, d2 = self.h.apply("package_import", DISP1, {"package": package()})
        self.assertEqual(d1["id"], d2["id"])
        self.assertTrue(d2["idempotent_replay"])
        _, ok = self.h.decide(d1["id"], DISP2)
        self.assertEqual(ok["status"], "executed")
        # 重复批准 → 幂等回放（不二次执行业务）
        _, again = self.h.decide(d1["id"], DISP2)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["status"], "executed")
        self.assertEqual(again["executed_at"], ok["executed_at"])
        # 另一调度再批同样回放
        _, again2 = self.h.decide(d1["id"], DISP2)
        self.assertEqual(again2["status"], "executed")
        # 相同执行包再申请：同指纹命中已终局事项 → 幂等回放（不新建、不二次执行）
        _, d3 = self.h.apply("package_import", DISP1, {"package": package()})
        self.assertEqual(d3["id"], d1["id"])
        self.assertTrue(d3["idempotent_replay"])
        self.assertEqual(d3["status"], "executed")
        _, man = self.h.call("GET", "/terminal/manifest")  # 终端免登录
        self.assertEqual(len(man["batches"]), 1)

    # ---- 5. 并发批准只有一次生效 ----
    def test_07_concurrent_approval_only_one_wins(self):
        _, d = self.h.apply("package_import", DISP1, {"package": package()})
        outcomes, errs = [], []

        def approve(user):
            try:
                _, r = self.h.decide(d["id"], user)
                outcomes.append((user, r["status"], r.get("idempotent_replay")))
            except AssertionError as e:
                errs.append(str(e))

        # 用两个不同的调度账号同时批准
        # 再造一个调度账号（角色调整接口）
        self.h.call("POST", "/accounts",
                    {"username": "disp3", "display_name": "调度3",
                     "roles": ["observe", "dispatch"]}, user=DISP1)
        threads = [threading.Thread(target=approve, args=(u,))
                   for u in (DISP2, "disp3")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertFalse(errs, errs)
        self.assertEqual(len(outcomes), 2)
        statuses = [o[1] for o in outcomes]
        self.assertIn("executed", statuses)
        # 第二个到达的批准一定是幂等回放，不会重复执行
        self.assertTrue(any(o[2] for o in outcomes))
        self.assertEqual(sum(1 for o in outcomes if not o[2]), 1)
        _, man = self.h.call("GET", "/terminal/manifest")
        self.assertEqual(len(man["batches"]), 1)

    # ---- 6. 资源版本：申请后目标状态变化 → 失效；导入/重试/封存 ----
    def test_08_resource_change_invalidates_retry_request(self):
        import_pkg(self.h)
        self.h.configure({"B-001:1": "http_503"})
        forward_receipts(self.h, "B-001", [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        # 申请失败重试（尚未批准）
        _, d = self.h.apply("manual_retry", DISP1,
                            {"batch_ref": "B-001", "seq": 1, "note": "重试"})
        self.assertEqual(d["status"], "pending")
        # 状态变化：另一调度批准一条 ack-terminal 无关操作不可行；
        # 直接让适配器恢复并由后台退避自然重发成功（资源状态改变 → 版本变化）
        self.h.configure({"*": "ack"}, reset_calls=False)
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded", message="退避后自然转发成功")
        # 此时批准旧申请 → 事项已失效
        code, err = self.h.decide(d["id"], DISP2, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "approval_invalidated")
        self.assertEqual(err["reason"], "resource_changed")
        ap = self.h.get_ap(d["id"])
        self.assertEqual(ap["status"], "invalid")
        self.assertEqual(ap["invalid_reason"], "resource_changed")

    def test_09_resource_changed_immediately_invalidated_in_list(self):
        import_pkg(self.h)
        self.h.configure({"*": "ack"})
        forward_receipts(self.h, "B-001", [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        # 就 seq2 申请重试（queued 态会立即被转发，状态变化）
        forward_receipts(self.h, "B-001", [2])
        _, d = self.h.apply("manual_retry", DISP1,
                            {"batch_ref": "B-001", "seq": 2})
        # 后台转发推进 → 未决事项立即失效
        self.h.wait_until(
            lambda: self.h.get_ap(d["id"])["status"] == "invalid",
            message="资源变化即时失效")
        ap = self.h.get_ap(d["id"])
        self.assertEqual(ap["invalid_reason"], "resource_changed")

    # ---- 7. 账号停用 / 角色调整 ----
    def test_10_disable_and_role_change_invalidate(self):
        _, d = self.h.apply("package_import", DISP1, {"package": package()})
        # 停用申请人 → 未决事项立即失效
        self.h.call("POST", f"/accounts/{DISP1}/active",
                    {"active": False}, user=ARCH1)
        ap = self.h.get_ap(d["id"])
        self.assertEqual(ap["status"], "invalid")
        self.assertEqual(ap["invalid_reason"], "account_disabled")
        code, err = self.h.decide(d["id"], DISP2, expect_error=True)
        self.assertEqual(code, 409)
        # 被停用账号无法再操作
        code, err = self.h.call("GET", "/batches", user=DISP1, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(err["code"], "account_disabled")

    def test_11_role_downgrade_invalidates_only_affected(self):
        # DISP1 同时有两个未决申请（导入两个批次）
        _, d1 = self.h.apply("package_import", DISP1,
                             {"package": package("B-A1")})
        _, d2 = self.h.apply("package_import", DISP1,
                             {"package": package("B-A2")})
        # 收缩 DISP1 为仅观察 → 两个未决申请都失效
        self.h.call("POST", "/accounts",
                    {"username": DISP1, "roles": ["observe"]}, user=ARCH1)
        self.assertEqual(self.h.get_ap(d1["id"])["status"], "invalid")
        self.assertEqual(self.h.get_ap(d2["id"])["status"], "invalid")
        for ap_id in (d1["id"], d2["id"]):
            self.assertEqual(self.h.get_ap(ap_id)["invalid_reason"],
                             "role_changed")

    def test_12_already_effected_action_not_rolled_back(self):
        # 批准导入生效；随后停用申请人，执行包仍然存在（不回滚）
        import_pkg(self.h, ref="B-KEEP")
        self.h.call("POST", f"/accounts/{DISP1}/active", {"active": False},
                    user=ARCH1)
        _, man = self.h.call("GET", "/terminal/manifest")
        refs = {b["batch_ref"] for b in man["batches"]}
        self.assertIn("B-KEEP", refs)

    # ---- 8. 超时失效 ----
    def test_13_timeout_invalidates_pending(self):
        h = ApprovalHarness(ttl=0.3)
        try:
            _, d = h.apply("package_import", DISP1, {"package": package("B-T1")})
            self.assertEqual(d["status"], "pending")
            time.sleep(0.5)
            # 再批准时事项已超时失效
            code, err = h.decide(d["id"], DISP2, expect_error=True)
            self.assertEqual(code, 409)
            self.assertIn(err["code"], ("approval_expired", "approval_invalidated"))
            self.assertEqual(err.get("reason", "timeout"), "timeout")
            ap = h.get_ap(d["id"])
            self.assertEqual(ap["status"], "invalid")
            self.assertEqual(ap["invalid_reason"], "timeout")
        finally:
            h.stop()
        os.environ.pop("RELAY_APPROVAL_TTL", None)

    # ---- 9. 批准不绕过业务校验 ----
    def test_14_seal_executes_and_rejects_ineligible_batch(self):
        import_pkg(self.h)
        self.h.configure({"*": "ack"})
        forward_receipts(self.h, "B-001", [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")
        # 冻结后台转发，制造一个稳定的「有缺口+缓冲项」不可封存状态
        self.h.app.forwarder.stop()
        _, up = self.h.call("POST", "/receipts/batch",
                            {"batch_ref": "B-001", "batch_summary": SUMMARY,
                             "receipts": [receipt(3, "R-3", "arrived")]}, user=OBS)
        self.assertEqual(up["results"][0]["state"], "buffered")
        _, d = self.h.apply("batch_seal", ARCH1,
                            {"batch_ref": "B-001", "note": "月底封存"})
        # 状态稳定（转发器已停）：批准触发原业务校验 → 拒绝，而非资源已变
        code, err = self.h.decide(d["id"], ARCH2, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "batch_not_sealable")
        # 事项标记为 execute_failed（批准留痕），批次未封存
        ap = self.h.get_ap(d["id"])
        self.assertEqual(ap["status"], "execute_failed")
        self.assertEqual(ap["execution_result_obj"]["code"], "batch_not_sealable")
        _, storage = self.h.call("GET", "/admin/storage", user=ARCH1)
        b = next(x for x in storage["batches"] if x["batch_ref"] == "B-001")
        self.assertFalse(b["archived"])

        # 恢复转发；业务满足（补齐缺口全部 forwarded）后重新申请→批准，封存成功
        self.h.app.forwarder.start()
        forward_receipts(self.h, "B-001", [2])
        self.h.wait_until(lambda: self.h.state()["cursor"] == 4)
        _, d_ok = self.h.apply("batch_seal", ARCH1,
                               {"batch_ref": "B-001", "note": "封存-重提"})
        _, ok = self.h.decide(d_ok["id"], ARCH2)
        self.assertEqual(ok["status"], "executed")
        self.assertTrue(ok["execution_result_obj"]["sealed"])
        _, archives = self.h.call("GET", "/archives", user=ARCH1)
        self.assertEqual(len(archives["archives"]), 1)

    def test_15_ack_terminal_requires_approval_and_executes(self):
        import_pkg(self.h)
        self.h.configure({"B-001:1": "batch_withdrawn", "*": "ack"})
        forward_receipts(self.h, "B-001", [1, 2])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "rejected_terminal")
        # 直调 ack-terminal 被拒
        code, err = self.h.call(
            "POST", "/batches/B-001/receipts/1/ack-terminal",
            {"note": "已解除"}, user=DISP1, expect_error=True)
        self.assertEqual(err["code"], "approval_required")
        # 主系统已解除，走双人复核
        self.h.configure({"*": "ack"}, reset_calls=False)
        _, ok = self.h.pair("ack_terminal", DISP1, DISP2,
                            {"batch_ref": "B-001", "seq": 1, "note": "撤回已恢复"})
        self.assertEqual(ok["status"], "executed")
        self.h.wait_until(lambda: self.h.state()["cursor"] == 3,
                          message="恢复连续转发")
        self.assertEqual(self.h.state()["counts"]["forwarded"], 2)

    def test_16_approve_failed_business_leaves_no_effect_and_not_replayable(self):
        # execute_failed 的事项不能被再次批准「补救」（决策已终局）
        import_pkg(self.h)
        self.h.configure({"B-001:1": "http_503"})
        forward_receipts(self.h, "B-001", [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        # 冻结转发器，使「待重试」这一不可封存原因稳定
        self.h.app.forwarder.stop()
        _, d = self.h.apply("batch_seal", ARCH1, {"batch_ref": "B-001"})
        code, err = self.h.decide(d["id"], ARCH2, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["code"], "batch_not_sealable")
        # 再次批准返回幂等回放（execute_failed 终局，不重新执行业务）
        _, again = self.h.decide(d["id"], ARCH2)
        self.assertEqual(again["status"], "execute_failed")
        self.assertTrue(again["idempotent_replay"])
        # 批次始终未封存
        _, storage = self.h.call("GET", "/admin/storage", user=ARCH1)
        b = next(x for x in storage["batches"] if x["batch_ref"] == "B-001")
        self.assertFalse(b["archived"])

    # ---- 10. 重启恢复 ----
    def test_17_restart_recovers_approvals_and_decisions(self):
        _, d = self.h.apply("package_import", DISP1, {"package": package("B-R1")})
        ap_id = d["id"]
        # 再造一个已生效事项用于验证完整恢复
        _, d2 = self.h.apply("package_import", DISP2, {"package": package("B-R2")})
        self.h.decide(d2["id"], DISP1)
        self.assertEqual(self.h.get_ap(d2["id"])["status"], "executed")

        db_path, tmp_dir, adapter = self.h.db_path, self.h.tmp, self.h.adapter
        self.h.stop(keep_data=True)
        self.h.tmp = None
        h2 = ApprovalHarness(db_path=db_path, adapter=adapter)
        h2.tmp = tmp_dir
        try:
            ap = h2.get_ap(ap_id, user=DISP1)
            self.assertEqual(ap["status"], "pending")  # 未决事项恢复
            self.assertFalse(ap["can_approve"])        # 申请人不能自批
            ap_as_disp2 = h2.call("GET", f"/approvals/{ap_id}", user=DISP2)[1]
            self.assertTrue(ap_as_disp2["can_approve"])
            # 新实例仍强制双人复核：批准后执行
            _, ok = h2.decide(ap_id, DISP2)
            self.assertEqual(ok["status"], "executed")
            # 已生效事项与决策记录完整
            _, dec = h2.call("GET", "/approval-decisions", user=OBS)
            ids = {x["approval_id"] for x in dec["decisions"]}
            self.assertIn(ap_id, ids)
            self.assertIn(d2["id"], ids)
            kinds = {(x["approval_id"], x["kind"]) for x in dec["decisions"]}
            self.assertIn((d2["id"], "executed"), kinds)
        finally:
            h2.stop()

    # ---- 11. 本人待办 / 已失效列表 / 完整决策记录 ----
    def test_18_todo_invalidated_list_and_decisions(self):
        _, d1 = self.h.apply("package_import", DISP1, {"package": package("B-TA")})
        _, d2 = self.h.apply("package_import", DISP2, {"package": package("B-TB")})
        # DISP1 的待办：自己申请 1 项 + 待他复核（DISP2 申请）1 项
        _, todo1 = self.h.call("GET", "/approvals/todo", user=DISP1)
        mine = {x["id"] for x in todo1["my_requests"]}
        review = {x["id"] for x in todo1["awaiting_my_review"]}
        self.assertIn(d1["id"], mine)
        self.assertIn(d2["id"], review)
        self.assertEqual(todo1["total"], 2)
        # 批准 d2（DISP1 复核 DISP2 的申请）
        self.h.decide(d2["id"], DISP1)
        # 失效 d1（停用 DISP1 后再启用；事项已失效但记录保留）
        self.h.call("POST", f"/accounts/{DISP1}/active", {"active": False},
                    user=ARCH1)
        self.h.call("POST", f"/accounts/{DISP1}/active", {"active": True},
                    user=ARCH1)
        _, inv = self.h.call(
            "GET", "/approvals?status=invalid,pending,executed,execute_failed,rejected",
            user=OBS)
        by_id = {x["id"]: x for x in inv["approvals"]}
        self.assertEqual(by_id[d1["id"]]["status"], "invalid")
        self.assertEqual(by_id[d2["id"]]["status"], "executed")
        # 只看自己的
        _, mine_list = self.h.call("GET", "/approvals?mine=1", user=DISP2)
        self.assertTrue(all(x["requested_by"] == DISP2
                            for x in mine_list["approvals"]))
        # 完整决策记录包含 d1 的 requested/invalidated 与 d2 的全部四态
        _, dec = self.h.call("GET", "/approval-decisions", user=OBS)
        per = {}
        for x in dec["decisions"]:
            per.setdefault(x["approval_id"], set()).add(x["kind"])
        self.assertEqual(per[d1["id"]], {"requested", "invalidated"})
        self.assertEqual(per[d2["id"]], {"requested", "approved", "executed"})

    def test_19_reject_records_decision_and_allows_reapply(self):
        _, d = self.h.apply("package_import", DISP1, {"package": package("B-X1")})
        _, r = self.h.decide(d["id"], DISP2, decision="reject", note="材料不全")
        self.assertEqual(r["status"], "rejected")
        ap = self.h.get_ap(d["id"])
        self.assertEqual(ap["decided_by"], DISP2)
        # 被拒后同指纹再申请：原事项为 rejected（终局），但允许重新申请一次新事项
        # （指纹含申请人，此处换备注不影响指纹 → 仍命中旧事项；故换参数生成新指纹）
        pkg2 = package("B-X1")
        pkg2["package_id"] = "PKG-CHANGED"
        _, d2 = self.h.apply("package_import", DISP1, {"package": pkg2})
        self.assertNotEqual(d["id"], d2["id"])
        _, ok = self.h.decide(d2["id"], DISP2)
        self.assertEqual(ok["status"], "executed")

    # ---- 12. 调度不能封存；归档审查不能重试（岗位分离）----
    def test_20_role_separation(self):
        # 调度申请封存 → 403
        code, err = self.h.call(
            "POST", "/approvals",
            {"action": "batch_seal", "batch_ref": "B-001"},
            user=DISP1, expect_error=True)
        self.assertEqual(err["code"], "forbidden_role")
        # 归档审查申请重试 → 403
        code, err = self.h.call(
            "POST", "/approvals",
            {"action": "manual_retry", "batch_ref": "B-001", "seq": 1},
            user=ARCH1, expect_error=True)
        self.assertEqual(err["code"], "forbidden_role")

    # ---- 13. manual_retry 双人复核全链路（立即重试生效）----
    def test_21_manual_retry_through_approval(self):
        import_pkg(self.h)
        self.h.configure({"B-001:1": "http_503"})
        forward_receipts(self.h, "B-001", [1])
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "retry_wait")
        # 冻结转发器后再申请，避免退避自然重试在复核前改变批次状态
        self.h.app.forwarder.stop()
        self.h.configure({"*": "ack"}, reset_calls=False)
        _, ok = self.h.pair("manual_retry", DISP1, DISP2,
                            {"batch_ref": "B-001", "seq": 1, "note": "立即重试"})
        self.assertEqual(ok["status"], "executed")
        self.assertEqual(ok["execution_result_obj"]["retried"], [1])
        self.h.app.forwarder.start()
        self.h.wait_until(lambda: self.h.receipt_state("B-001", 1)[0]["state"]
                          == "forwarded")


    # ---- 14. 崩溃在「已批准未执行」窗口：重启不自动补执行 ----
    def test_22_crash_between_approve_and_execute_not_auto_completed(self):
        import_pkg(self.h)
        _, d = self.h.apply("manual_retry", DISP1,
                            {"batch_ref": "B-001", "seq": 1, "note": "x"})
        # 手工把事项置为 approved（模拟 CAS 已提交、业务尚未执行时进程崩溃）
        with self.h.app.db.lock:
            self.h.app.db.conn.execute(
                "UPDATE approvals SET status='approved',decided_by=? WHERE id=?",
                (DISP2, d["id"]))
            self.h.app.db.commit()
        db_path, tmp_dir = self.h.db_path, self.h.tmp
        self.h.stop(keep_data=True)
        self.h.tmp = None
        h2 = ApprovalHarness(db_path=db_path,
                             adapter=adapters.StubCallbackAdapter())
        h2.tmp = tmp_dir
        try:
            self.assertGreaterEqual(h2.app.startup["expired_approvals"], 1)
            ap = h2.get_ap(d["id"], user=DISP2)
            self.assertEqual(ap["status"], "invalid")
            self.assertEqual(ap["invalid_reason"], "interrupted")
            # 业务未被自动补执行（执行包之外没有任何回执/封存等副作用）
            _, st = h2.call("GET", "/admin/storage", user=ARCH1)
            b = next(x for x in st["batches"] if x["batch_ref"] == "B-001")
            self.assertFalse(b["archived"])
            # 允许用同指纹重新申请
            _, d2 = h2.apply("manual_retry", DISP1,
                             {"batch_ref": "B-001", "seq": 1, "note": "x"})
            self.assertNotEqual(d["id"], d2["id"])
            self.assertEqual(d2["status"], "pending")
        finally:
            h2.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
