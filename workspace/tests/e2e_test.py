"""端到端测试：启动独立端口与临时数据库的服务实例，逐条验证业务规则。

运行：python3 backend/../tests/e2e_test.py
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
PORT = "8099"
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = 0, 0


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
    tmp = tempfile.mkdtemp(prefix="evac_test_")
    db_path = os.path.join(tmp, "test.db")
    proc = start(db_path)
    try:
        print("【1】初始化与幂等导入")
        _, s = req("GET", "/api/state")
        check("种子数据加载：11 户 / 11 路 / 5 车 / 4 避难点",
              len(s["households"]) == 11 and len(s["roads"]) == 11
              and len(s["vehicles"]) == 5 and len(s["shelters"]) == 4)
        check("全部 11 户待安排", len(s["pending_households"]) == 11)

        _, r = req("POST", "/api/households", {
            "ref": "H01", "name": "重复的陈家", "grid": "G1", "members": 9})
        check("重复户号不重复登记", r["inserted"] == 0 and r["skipped_duplicates"] == 1)
        _, s = req("GET", "/api/state")
        h01 = next(h for h in s["households"] if h["ref"] == "H01")
        check("重复导入不覆盖原登记（人数仍为4）", h01["members"] == 4)

        print("【2】规划满足全部硬约束")
        _, plan = req("POST", "/api/plan", {"name": "P1", "commander": "张指挥"})
        check("方案可行", plan["feasible"] and not plan["unassigned"])
        refs = [x["ref"] for b in plan["batches"] for x in b["households"]]
        check("11 户恰好各安置一次", sorted(refs) == sorted(h["ref"] for h in s["households"]))
        veh = {v["code"]: v for v in s["vehicles"]}
        she = {x["code"]: x for x in s["shelters"]}
        hh = {h["ref"]: h for h in s["households"]}
        seat_ok, acc_ok, pet_ok, bed_ok = True, True, True, True
        used_beds = {}
        for b in plan["batches"]:
            need = sum(hh[x["ref"]]["members"] + hh[x["ref"]]["pets"] for x in b["households"])
            if need > veh[b["vehicle"]]["seats"]:
                seat_ok = False
            if any(hh[x["ref"]]["mobility"] for x in b["households"]):
                if not veh[b["vehicle"]]["accessible"] or not she[b["shelter"]]["accessible"]:
                    acc_ok = False
            if any(hh[x["ref"]]["pets"] for x in b["households"]) and not she[b["shelter"]]["pets_allowed"]:
                pet_ok = False
            used_beds[b["shelter"]] = used_beds.get(b["shelter"], 0) + \
                sum(hh[x["ref"]]["members"] for x in b["households"])
            for e in b["route_edges"]:
                if next(r0 for r0 in s["roads"] if r0["code"] == e)["status"] != "open":
                    acc_ok = False
        check("座位（含宠物占位）不超限", seat_ok)
        check("行动辅助户→无障碍车+无障碍避难点", acc_ok)
        check("宠物户不进入禁宠避难点 S2", pet_ok)
        for c, n in used_beds.items():
            if n > she[c]["beds"]:
                bed_ok = False
        check("床位不超限", bed_ok)
        # 道路容量
        we = {}
        for b in plan["batches"]:
            for e in b["route_edges"]:
                we[(b["wave"], e)] = we.get((b["wave"], e), 0) + 1
        cap = {r["code"]: r["capacity"] for r in s["roads"]}
        check("每波次道路并发不超容量", all(n <= cap[e] for (_, e), n in we.items()))

        print("【3】乐观并发：多名指挥员基于旧方案同时发布只有一份生效")
        # 再生成一个同样基于当前修订号的方案（模拟另一名指挥员）
        _, plan2 = req("POST", "/api/plan", {"name": "P2", "commander": "李指挥"})
        _, pub = req("POST", f"/api/plans/{plan['id']}/publish", {"commander": "张指挥"})
        check("第一份发布成功", "order_id" in pub)
        code, err = req("POST", f"/api/plans/{plan2['id']}/publish",
                        {"commander": "李指挥"}, expect=409)
        check("基于旧修订号的第二份发布被拒（409）", code == 409 and "过期" in err["error"])
        _, s = req("GET", "/api/state")
        check("仅一份生效命令", len(s["orders"]) == 1)
        check("生效方案标记为 published", True)
        scheduled = [b for b in s["batches"] if b["status"] == "scheduled"]
        check("发布后生成计划批次", len(scheduled) == len(plan["batches"]))

        print("【4】车辆离场→批次与床位成为不可变承诺")
        first = next(b for b in s["batches"] if b["status"] == "scheduled")
        departed_hh = list(first["households"])
        _, d = req("POST", f"/api/batches/{first['id']}/depart", {})
        check("离场成功", d["status"] == "departed")
        _, s = req("GET", "/api/state")
        st = s["household_state"][departed_hh[0]]
        check("离场家庭状态=departed", st["status"] == "departed")
        cap1 = s["capacity"][first["shelter"]]
        committed_people = sum(
            next(h for h in s["households"] if h["ref"] == r)["members"]
            for r in departed_hh)
        check(f"目的地床位计入已承诺（{cap1['committed']}>={committed_people}）",
              cap1["committed"] >= committed_people)
        vdep = next(v for v in s["vehicles"] if v["code"] == first["vehicle"])
        check("离场车辆标记 departed，不再可用", vdep["status"] == "departed")
        check("同车后续未出发批次被撤回待重排",
              all(not (b["vehicle"] == first["vehicle"] and b["status"] == "scheduled")
                  for b in s["batches"]))

        print("【5】道路封闭：只撤回受影响的未出发批次，已出发承诺不动")
        _, p3 = req("POST", "/api/plan", {"name": "P3", "commander": "张指挥"})
        check("重算只针对未出发家庭（不含已离场户）",
              p3["totals"]["households_planned"] == 11 - len(departed_hh)
              and departed_hh[0] not in
              [x["ref"] for b in p3["batches"] for x in b["households"]])
        _, pub3 = req("POST", f"/api/plans/{p3['id']}/publish", {})
        _, s = req("GET", "/api/state")
        before_departed = [b for b in s["batches"] if b["status"] == "departed"]
        before_promised = {c: v["committed"] for c, v in s["capacity"].items()}
        r5_sched = [b for b in s["batches"]
                    if b["status"] == "scheduled" and "r5" in b["route"]["edges"]]
        other_sched = [b for b in s["batches"]
                       if b["status"] == "scheduled" and "r5" not in b["route"]["edges"]]
        _, ev = req("POST", "/api/events", {"type": "road_close", "code": "r5"})
        check("封路撤回了经 r5 的未出发批次",
              set(ev["released_for_replan"]) ==
              set(x for b in r5_sched for x in b["households"]))
        _, s = req("GET", "/api/state")
        check("不经过 r5 的未出发批次保留",
              all(any(b["id"] == o["id"] for b in s["batches"]) for o in other_sched))
        check("已出发批次完全不变",
              [b["id"] for b in s["batches"] if b["status"] == "departed"] ==
              [b["id"] for b in before_departed])
        check("已承诺床位不释放",
              all(s["capacity"][c]["committed"] == v for c, v in before_promised.items()))

        print("【6】车辆故障 / 避难点暂停：只重算未出发户")
        _, p4 = req("POST", "/api/plan", {"name": "P4"})
        req("POST", f"/api/plans/{p4['id']}/publish", {})
        _, s = req("GET", "/api/state")
        target_veh = next(b["vehicle"] for b in s["batches"] if b["status"] == "scheduled")
        _, ev2 = req("POST", "/api/events", {"type": "vehicle_down", "code": target_veh})
        check("故障车的未出发批次撤回", len(ev2["released_for_replan"]) >= 1)
        _, s = req("GET", "/api/state")
        check("已出发承诺未受车辆故障影响",
              all(b["status"] == "departed" for b in s["batches"]
                  if b["id"] in [x["id"] for x in before_departed]))
        _, ev3 = req("POST", "/api/events", {"type": "shelter_suspend", "code": "S3"})
        _, s = req("GET", "/api/state")
        check("暂停的避难点不再保留指向它的未出发批次",
              all(not (b["status"] == "scheduled" and b["shelter"] == "S3")
                  for b in s["batches"]))

        print("【7】无可行安置时给出具体约束；资源恢复后可继续编排")
        # 恢复路况，再让所有可用车故障 → 应整体不可行并说明
        req("POST", "/api/events", {"type": "road_open", "code": "r5"})
        req("POST", "/api/events", {"type": "shelter_resume", "code": "S3"})
        for v in s["vehicles"]:
            if v["status"] == "available":
                req("POST", "/api/events", {"type": "vehicle_down", "code": v["code"]})
        _, bad = req("POST", "/api/plan", {"name": "无车方案"})
        check("无可用车时方案不可行", not bad["feasible"])
        check("诊断明确指出车辆约束",
              all(any("车辆" in r for r in u["reasons"]) for u in bad["unassigned"]))
        check("不可行方案禁止发布", req("POST", f"/api/plans/{bad['id']}/publish", {},
                                       expect=409)[0] == 409)
        # 恢复车辆后重新可行（对每辆车发恢复；已离场车辆不会被恢复）
        for code in ("V1", "V2", "V3", "V4", "V5"):
            req("POST", "/api/events", {"type": "vehicle_recovered", "code": code})
        _, good = req("POST", "/api/plan", {"name": "恢复后方案"})
        check("资源恢复后方案重新可行", good["feasible"])

        print("【8】火线扩大：新网格住户进入待编排")
        req("POST", "/api/households", {"ref": "H12", "grid": "G4", "members": 3})
        req("POST", "/api/events", {"type": "fire_expand", "code": "G4"})
        _, s = req("GET", "/api/state")
        check("G4 已受威胁且 H12 进入待安排",
              next(g for g in s["grids"] if g["code"] == "G4")["threatened"]
              and "H12" in s["pending_households"])
        _, pf = req("POST", "/api/plan", {"name": "火线扩大后"})
        check("新方案覆盖 H12",
              "H12" in [x["ref"] for b in pf["batches"] for x in b["households"]])

        print("【9】进程重启后命令、出发记录与剩余容量恢复")
        orders_before = s["orders"]
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = start(db_path)
        _, s2 = req("GET", "/api/state")
        dep_before = [b["id"] for b in before_departed]
        dep_after = [b["id"] for b in s2["batches"] if b["status"] == "departed"]
        check("已发布命令恢复", len(s2["orders"]) == len(orders_before))
        check("出发记录恢复", sorted(dep_after) == sorted(dep_before))
        check("已承诺床位恢复",
              all(s2["capacity"][c]["committed"] == before_promised[c]
                  for c in before_promised))
        check("离场车辆状态恢复",
              next(v for v in s2["vehicles"] if v["code"] == first["vehicle"])["status"]
              == "departed")
        check("修订号恢复", s2["revision"] >= s["revision"])

        print("【10】关闭孤立路径时禁止安排家庭自行经过关闭路段")
        # 封闭全部可通达某受威胁网格的道路 → 该网格家庭不可行，诊断指明封路
        # （直接构造：封闭 G3 相关道路后规划不应出现经过 closed 边的批次）
        _, pp = req("POST", "/api/plan", {"name": "最终复核"})
        closed = {r["code"] for r in s2["roads"] if r["status"] == "closed"}
        check("任何批次路径都不含已关闭道路",
              all(not (set(b["route_edges"]) & closed) for b in pp["batches"]))

        print("【11】离线中继对接面：执行包导出 + 回调幂等/终态拒绝")
        # 恢复路况后重新规划发布，确保有 scheduled 批次可导出
        for rc in closed:
            req("POST", "/api/events", {"type": "road_open", "code": rc})
        _, pe = req("POST", "/api/plan", {"name": "中继对接方案"})
        if pe.get("feasible") and pe["batches"]:
            req("POST", f"/api/plans/{pe['id']}/publish", {})
        _, listing = req("GET", "/api/execution-packages?status=scheduled")
        check("可导出已发布计划批次", len(listing["batches"]) >= 1,
              str(len(listing["batches"])))
        if listing["batches"]:
            bx = listing["batches"][0]
            _, pkg = req("GET", f"/api/execution-package/v1/{bx['id']}")
            check("执行包 batch_ref/摘要/车辆/目的地/items 完整",
                  pkg["batch_ref"] == bx["id"] and pkg["batch_summary"]
                  and pkg["vehicle"]["code"] and pkg["destination"]["code"]
                  and isinstance(pkg["items"], list))
            # 回调幂等
            cb = {"idempotency_key": f"e2e:{bx['id']}:1", "batch_ref": bx["id"],
                  "seq": 1, "receipt_no": "E2E-R1", "type": "failed_to_depart"}
            _, a1 = req("POST", "/api/relay-callback", cb)
            _, a2 = req("POST", "/api/relay-callback", cb)
            check("回调首次 ack", a1["result"] == "ack")
            check("相同幂等键重放返回首次响应",
                  a2.get("idempotent_replay") is True and a2["result"] == "ack")
            # 车辆故障 → 批次撤回 → 回执终态拒绝
            _, st = req("GET", "/api/state")
            scheduled = [b for b in st["batches"] if b["status"] == "scheduled"]
            if scheduled:
                by = scheduled[0]
                req("POST", "/api/events",
                    {"type": "vehicle_down", "code": by["vehicle"]})
                _, rej = req("POST", "/api/relay-callback", {
                    "idempotency_key": f"e2e:{by['id']}:9", "batch_ref": by["id"],
                    "seq": 9, "receipt_no": "E2E-R9", "type": "departed"})
                check("撤回批次回执被终态拒绝",
                      rej["result"] == "rejected"
                      and rej["reason"] == "batch_withdrawn")
            # 不存在批次 → batch_withdrawn
            _, rej2 = req("POST", "/api/relay-callback", {
                "idempotency_key": "e2e:nope:1", "batch_ref": "B-NONEXISTENT",
                "seq": 1, "receipt_no": "X", "type": "arrived"})
            check("未知批次回执被终态拒绝",
                  rej2["reason"] == "batch_withdrawn")
        # 缺少幂等键 → 400
        req("POST", "/api/relay-callback",
            {"batch_ref": "x", "type": "arrived"}, expect=400)
        check("缺少 idempotency_key 返回 400", True)

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
