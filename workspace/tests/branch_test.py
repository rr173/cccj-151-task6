"""推演分支端到端测试：独立端口 + 临时库，验证隔离、幂等、三方提升与重启恢复。

运行：python3 tests/branch_test.py
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "backend")
PORT = "8101"
BASE = f"http://127.0.0.1:{PORT}"

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def req(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())
        if e.code != expect:
            raise AssertionError(f"{method} {path} 预期 {expect}，实际 {e.code}: {payload}")
        return e.code, payload


def start(db_path):
    env = dict(os.environ, EVAC_PORT=PORT, EVAC_DB=db_path, EVAC_NO_SEED="0")
    p = subprocess.Popen([sys.executable, os.path.join(BACKEND, "server.py")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            req("GET", "/api/state")
            return p
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("服务启动失败")


def main():
    tmp = tempfile.mkdtemp(prefix="evac_branch_")
    db_path = os.path.join(tmp, "test.db")
    proc = start(db_path)
    try:
        print("【1】创建分支：完整复制当前态势，幂等")
        _, b1 = req("POST", "/api/branches", {"name": "B1", "idem_key": "key-1"})
        bid = b1["id"]
        check("创建返回基线修订号 0", b1["base_live_revision"] == 0)
        _, b1b = req("POST", "/api/branches", {"name": "B1", "idem_key": "key-1"})
        check("相同幂等键返回同一分支", b1b["id"] == bid and b1b["idempotent_replay"])
        _, live = req("GET", "/api/state")
        _, bst = req("GET", f"/api/branches/{bid}/state")
        check("分支态势与实战一致（道路/户数）",
              len(bst["roads"]) == len(live["roads"])
              and len(bst["households"]) == len(live["households"]))
        check("分支状态带上下文标识",
              bst["context"]["kind"] == "branch" and bst["context"]["id"] == bid)

        print("【2】分支模拟与实战物理隔离")
        req("POST", f"/api/branches/{bid}/events",
            {"type": "road_close", "code": "r5", "op_id": "e1"})
        _, replay = req("POST", f"/api/branches/{bid}/events",
                        {"type": "road_close", "code": "r5", "op_id": "e1"})
        check("模拟写操作按 op_id 幂等", replay.get("idempotent_replay") is True)
        _, live = req("GET", "/api/state")
        check("实战 r5 仍开放（推演未污染实战）",
              next(r for r in live["roads"] if r["code"] == "r5")["status"] == "open")
        _, bst = req("GET", f"/api/branches/{bid}/state")
        check("分支 r5 已封闭",
              next(r for r in bst["roads"] if r["code"] == "r5")["status"] == "closed")
        check("实战修订号未被推演改动", live["revision"] == 0)
        check("分支修订号已推进", bst["revision"] == 1)

        print("【3】分支内规划/发布/离场均隔离，不产生实战命令与承诺")
        _, p = req("POST", f"/api/branches/{bid}/plan", {"name": "bp"})
        check("分支可重新规划", p["feasible"])
        req("POST", f"/api/branches/{bid}/plans/{p['id']}/publish", {"commander": "推演员"})
        _, bst = req("GET", f"/api/branches/{bid}/state")
        batch = next(b for b in bst["batches"] if b["status"] == "scheduled")
        req("POST", f"/api/branches/{bid}/batches/{batch['id']}/depart", {})
        _, live = req("GET", "/api/state")
        check("实战无任何命令", len(live["orders"]) == 0)
        check("实战车辆未被推演离场影响",
              next(v for v in live["vehicles"] if v["code"] == batch["vehicle"])["status"]
              == "available")

        print("【4】并排差异：字段/人员/床位/受阻原因")
        _, diff = req("GET", f"/api/branches/{bid}/diff")
        r5f = next(f for f in diff["resource_fields"] if f["code"] == "r5")
        check("r5 差异判定 branch_only（可提升）", r5f["status"] == "branch_only"
              and r5f["live"] == "open" and r5f["branch"] == "closed")
        vf = [f for f in diff["resource_fields"]
              if f["code"] == batch["vehicle"] and f["field"] == "status"]
        check("推演离场车辆标记为 commitment（不迁移）",
              vf and vf[0]["status"] == "commitment")
        check("存在人员去向差异", any(x["differs"] for x in diff["people"]))
        check("存在床位余量差异（推演承诺）", any(x["differs"] for x in diff["shelters"]))

        print("【5】提升：只应用干净资源改动；命令/离场/承诺不迁移")
        _, pr = req("POST", f"/api/branches/{bid}/promote",
                    {"expected_live_revision": diff["live_revision"], "promote_id": "pr1"})
        check("提升成功", pr["applied"] and pr["applied_fields"] == ["roads.r5.status=closed"])
        check("推演离场承诺被跳过",
              [c["code"] for c in pr["skipped_commitments"]] == [batch["vehicle"]])
        _, live = req("GET", "/api/state")
        check("实战 r5 已被提升为封闭",
              next(r for r in live["roads"] if r["code"] == "r5")["status"] == "closed")
        check("提升后实战仍无推演命令", len(live["orders"]) == 0)
        check("提升后实战车辆仍可用",
              next(v for v in live["vehicles"] if v["code"] == batch["vehicle"])["status"]
              == "available")
        check("受封路影响的实战未出发批次被撤回", isinstance(pr["released_for_replan"], list))

        print("【6】提升幂等")
        _, pr2 = req("POST", f"/api/branches/{bid}/promote",
                     {"expected_live_revision": 0, "promote_id": "pr1"})
        check("相同 promote_id 返回首次结果（幂等重放）",
              pr2.get("idempotent_replay") and pr2["applied"] is True)
        _, live2 = req("GET", "/api/state")
        check("幂等重放不重复推进修订号", live2["revision"] == live["revision"])

        print("【7】过期基线整体拒绝")
        code, stale = req("POST", f"/api/branches/{bid}/promote",
                          {"expected_live_revision": 99999, "promote_id": "pr-stale"},
                          expect=409)
        check("过期修订号 → 409 stale_baseline",
              code == 409 and stale["code"] == "stale_baseline")

        print("【8】三方冲突整体拒绝并列出对象与字段")
        _, b2 = req("POST", "/api/branches", {"name": "B2", "idem_key": "key-2"})
        bid2 = b2["id"]
        # 分支：V3 故障；实战：把 V3 变成 departed（真实离场流程）
        req("POST", f"/api/branches/{bid2}/events",
            {"type": "vehicle_down", "code": "V3", "op_id": "v3down"})
        _, lp = req("POST", "/api/plan", {"name": "live-plan"})
        req("POST", f"/api/plans/{lp['id']}/publish", {})
        _, ls = req("GET", "/api/state")
        v3b = next((b for b in ls["batches"]
                    if b["status"] == "scheduled" and b["vehicle"] == "V3"), None)
        if v3b:
            req("POST", f"/api/batches/{v3b['id']}/depart", {})
        _, ls = req("GET", "/api/state")
        rev_before = ls["revision"]
        code, conf = req("POST", f"/api/branches/{bid2}/promote",
                         {"expected_live_revision": ls["revision"], "promote_id": "pr-conf"},
                         expect=409)
        check("冲突 → 409 conflict", code == 409 and conf["code"] == "conflict")
        if v3b:
            check("列出冲突对象 vehicles:V3", "vehicles:V3" in conf["conflict_objects"])
            check("列出冲突字段与三侧取值",
                  any(c["code"] == "V3" and c["field"] == "status"
                      and c["base"] == "available" and c["live"] == "departed"
                      and c["branch"] == "down" for c in conf["conflicts"]))
        _, ls2 = req("GET", "/api/state")
        check("整体拒绝：实战修订号不变", ls2["revision"] == rev_before)
        # 分支里同时存在的其他干净改动也不得应用
        req("POST", f"/api/branches/{bid2}/events",
            {"type": "shelter_suspend", "code": "S1", "op_id": "s1"})
        _, ls3 = req("GET", "/api/state")
        code, conf2 = req("POST", f"/api/branches/{bid2}/promote",
                          {"expected_live_revision": ls3["revision"], "promote_id": "pr-conf2"},
                          expect=409)
        check("任一冲突则干净改动也不应用（整体拒绝）",
              next(s for s in ls3["shelters"] if s["code"] == "S1")["status"] == "active")
        check("拒绝结果提示待应用干净改动数", conf2["pending_clean_changes"] >= 1)

        print("【9】冲突不会回滚实战创建后新增的承诺（按创建基线判定）")
        # B1 的创建基线里 V3=available；实战后来 V3 离场。B1 未改 V3，再次提升不得回滚 V3。
        req("POST", f"/api/branches/{bid}/events",
            {"type": "vehicle_down", "code": "V4", "op_id": "v4"})
        _, ls = req("GET", "/api/state")
        _, pr3 = req("POST", f"/api/branches/{bid}/promote",
                     {"expected_live_revision": ls["revision"], "promote_id": "pr3"})
        check("提升成功且不含 V3 回滚",
              pr3["applied"] and "vehicles.V3.status=departed" not in pr3["applied_fields"]
              and "vehicles.V3.status=available" not in pr3["applied_fields"])
        _, ls = req("GET", "/api/state")
        check("实战 V3 离场承诺保持",
              next(v for v in ls["vehicles"] if v["code"] == "V3")["status"]
              == ("departed" if v3b else "down"))
        check("实战 V4 按推演故障",
              next(v for v in ls["vehicles"] if v["code"] == "V4")["status"] == "down")

        print("【10】废弃分支：幂等，禁止再写，但保留只读与审计")
        _, d1 = req("POST", f"/api/branches/{bid2}/discard", {})
        _, d2 = req("POST", f"/api/branches/{bid2}/discard", {})
        check("废弃幂等", d1["status"] == "discarded" and d2["idempotent_replay"])
        code, dd = req("POST", f"/api/branches/{bid2}/events",
                       {"type": "road_open", "code": "r5"}, expect=409)
        check("废弃分支禁止模拟写", code == 409 and dd["code"] == "discarded")
        code, _ = req("GET", f"/api/branches/{bid2}/diff")
        check("废弃分支差异仍可只读查看", code == 200)
        _, audit = req("GET", f"/api/branches/{bid2}/audit")
        check("审计记录含创建/模拟/拒绝提升/废弃",
              {"create", "simulate", "promote_rejected", "discard"} <=
              {a["action"] for a in audit})

        print("【11】重启后恢复分支历史、差异与审计")
        orders_before = len(req("GET", "/api/state")[1]["orders"])
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = start(db_path)
        _, brs = req("GET", "/api/branches")
        check("分支列表恢复（含状态）",
              {(b["id"], b["status"]) for b in brs} >=
              {(bid, "active"), (bid2, "discarded")})
        _, diff2 = req("GET", f"/api/branches/{bid}/diff")
        check("分支差异重启后可恢复", diff2["branch"]["id"] == bid)
        _, bev = req("GET", f"/api/branches/{bid}/events")
        check("分支模拟事件历史恢复", any(e["kind"] == "road_close" for e in bev))
        _, audit2 = req("GET", f"/api/branches/{bid}/audit")
        check("分支审计记录恢复", any(a["action"] == "promote_ok" for a in audit2))
        # 重启后幂等凭证仍生效
        _, replay_after = req("POST", f"/api/branches/{bid}/events",
                              {"type": "road_close", "code": "r5", "op_id": "e1"})
        check("op_id 幂等在重启后仍生效", replay_after.get("idempotent_replay") is True)
        _, pr_replay = req("POST", f"/api/branches/{bid}/promote",
                           {"expected_live_revision": 0, "promote_id": "pr1"})
        check("promote_id 幂等在重启后仍生效", pr_replay.get("idempotent_replay") is True)
        _, live = req("GET", "/api/state")
        check("实战命令数不受重启影响", len(live["orders"]) == orders_before)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
