"""中继 × 撤离编排系统 跨系统集成测试。

启动两个真实子进程：
  * 编排系统（seed 演示数据，EVAC_PORT）
  * 中继（RELAY_CALLBACK_ADAPTER=http，回调指向编排系统）

验证端到端契约：
  A. 从主系统导出执行包 → 导入中继（不可变/幂等）
  B. departed 回执经中继 HTTP 适配器转发 ack → 主系统批次形成离场承诺
  C. 主系统回调按 idempotency_key 幂等重放
  D. 批次撤回后转发 → 主系统 batch_withdrawn 终态拒绝 → 中继冻结游标、后续不越过
  E. 中继进程重启后队列继续，且已成功回调不重复（查主系统 relay_callbacks 台账）

运行：python3 relay/tests/integration_test.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BACKEND = os.path.join(ROOT, "backend")
RELAY_BACKEND = os.path.join(ROOT, "relay", "backend")

MAIN_PORT = 18091
RELAY_PORT = 18092
PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def wait_up(url, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.25)
    raise RuntimeError(f"服务未就绪: {url}")


def req(base, method, path, body=None, expect_error=False):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read().decode())
        if expect_error:
            return e.code, payload
        raise AssertionError(f"{method} {path} -> {e.code}: {payload}")


def wait_for(predicate, timeout=15, message=""):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except Exception as e:  # noqa: BLE001
            last = f"err:{e}"
        if last is True:
            return
        time.sleep(0.25)
    raise AssertionError(f"等待超时：{message}（last={last}）")


def start(script, env_extra, cwd, name):
    env = dict(os.environ, PYTHONUNBUFFERED="1", **env_extra)
    log = open(os.path.join(cwd, f"{name}.log"), "w")
    p = subprocess.Popen([sys.executable, script], env=env, cwd=cwd,
                         stdout=log, stderr=subprocess.STDOUT)
    return p


def main():
    tmp = tempfile.mkdtemp(prefix="relay-integ-")
    main_db = os.path.join(tmp, "evac.db")
    relay_db = os.path.join(tmp, "relay.db")
    procs = []
    try:
        # ---- 启动主系统 ----
        pm = start(os.path.join(BACKEND, "server.py"), {
            "EVAC_PORT": str(MAIN_PORT), "EVAC_DB": main_db,
            "EVAC_HOST": "127.0.0.1", "EVAC_NO_SEED": "0"}, BACKEND, "main")
        procs.append(("main", pm))
        wait_up(f"http://127.0.0.1:{MAIN_PORT}/api/state")

        # 规划并发布（演示数据有可行解）
        _, plan = req(f"http://127.0.0.1:{MAIN_PORT}", "POST", "/api/plan",
                      {"name": "集成测试方案", "commander": "张指挥"})
        assert plan["feasible"] and plan["batches"], "演示数据应可规划"
        req(f"http://127.0.0.1:{MAIN_PORT}", "POST",
            f"/api/plans/{plan['id']}/publish", {"commander": "张指挥"})

        # ---- 启动中继（真实 HTTP 适配器）----
        pr = start(os.path.join(RELAY_BACKEND, "server.py"), {
            "RELAY_PORT": str(RELAY_PORT), "RELAY_DB": relay_db,
            "RELAY_HOST": "127.0.0.1",
            "RELAY_CALLBACK_ADAPTER": "http",
            "RELAY_CALLBACK_URL": f"http://127.0.0.1:{MAIN_PORT}/api/relay-callback",
            "RELAY_TICK_SECONDS": "0.5",
            "RELAY_BACKOFF_BASE": "0.5"}, RELAY_BACKEND, "relay")
        procs.append(("relay", pr))
        wait_up(f"http://127.0.0.1:{RELAY_PORT}/relay/api/health")

        m = f"http://127.0.0.1:{MAIN_PORT}"
        r = f"http://127.0.0.1:{RELAY_PORT}/relay/api"

        # ---- A. 导出执行包并导入中继 ----
        print("A. 执行包导出/导入")
        _, listing = req(m, "GET", "/api/execution-packages?status=scheduled")
        batches = listing["batches"]
        check("主系统存在已发布计划批次", len(batches) >= 2, str(len(batches)))
        b1 = batches[0]
        _, pkg = req(m, "GET", f"/api/execution-package/v1/{b1['id']}")
        check("执行包 batch_ref=批次id", pkg["batch_ref"] == b1["id"])
        check("执行包携带摘要/车辆/目的地/items",
              pkg["batch_summary"] and pkg["vehicle"]["code"] == b1["vehicle_code"]
              and pkg["destination"]["code"] == b1["shelter_code"] and pkg["items"])

        code, imp = req(r, "POST", "/packages/import", {"package": pkg})
        check("首次导入 201", code == 201 and not imp["replay"])
        _, imp2 = req(r, "POST", "/packages/import", {"package": pkg})
        check("相同包再次导入幂等重放", imp2["replay"])

        # 第二个批次留作终态拒绝场景
        b2 = batches[1]
        _, pkg2 = req(m, "GET", f"/api/execution-package/v1/{b2['id']}")
        req(r, "POST", "/packages/import", {"package": pkg2})

        # ---- B. departed 转发 ack → 主系统离场承诺 ----
        print("B. departed 回执 → ack → 离场承诺")
        summary = pkg["batch_summary"]
        req(r, "POST", "/receipts/batch", {"batch_ref": b1["id"], "receipts": [
            {"seq": 1, "receipt_no": "RR-0001", "type": "departed",
             "batch_summary": summary, "at": "2026-10-02T08:00:00Z"},
            {"seq": 2, "receipt_no": "RR-0002", "type": "arrived",
             "batch_summary": summary, "at": "2026-10-02T09:10:00Z"},
        ]})

        def b1_forwarded():
            _, d = req(r, "GET", f"/batches/{b1['id']}")
            return d["counts"]["forwarded"] == 2 and d["cursor"] == 3
        wait_for(b1_forwarded, message="b1 两条回执连续转发")

        _, state = req(m, "GET", "/api/state")
        sb = next(x for x in state["batches"] if x["id"] == b1["id"])
        check("主系统批次变为 departed（离场承诺联动）", sb["status"] == "departed")

        # ---- C. 主系统回调幂等重放 ----
        print("C. 回调幂等")
        cb = {"idempotency_key": f"{b1['id']}:1:test", "batch_ref": b1["id"],
              "seq": 1, "receipt_no": "RR-0001", "type": "arrived"}
        _, a1 = req(m, "POST", "/api/relay-callback", cb)
        _, a2 = req(m, "POST", "/api/relay-callback", cb)
        check("重复回调返回首次响应", a2.get("idempotent_replay") is True
              and a2["result"] == a1["result"])

        # ---- D. 终态拒绝：车辆故障 → 未出发批次撤回 ----
        print("D. 终态拒绝与游标冻结")
        veh2 = b2["vehicle_code"]
        req(m, "POST", "/api/events", {"type": "vehicle_down", "code": veh2})
        _, st2 = req(m, "GET", "/api/state")
        b2now = next(x for x in st2["batches"] if x["id"] == b2["id"])
        check("b2 已被撤回", b2now["status"] == "cancelled", b2now["status"])

        summary2 = pkg2["batch_summary"]
        req(r, "POST", "/receipts/batch", {"batch_ref": b2["id"], "receipts": [
            {"seq": 1, "receipt_no": "RR-1001", "type": "departed",
             "batch_summary": summary2},
            {"seq": 2, "receipt_no": "RR-1002", "type": "arrived",
             "batch_summary": summary2},
        ]})

        def b2_blocked():
            _, d = req(r, "GET", f"/batches/{b2['id']}")
            row1 = next(x for x in d["receipts"] if x["seq"] == 1)
            return (row1["state"] == "rejected_terminal"
                    and row1["result_code"] == "batch_withdrawn"
                    and d["cursor"] == 1
                    and {x["seq"] for x in d["buffered_items"]} == {2})
        wait_for(b2_blocked, message="b1 seq1 终态拒绝且 seq2 缓冲不越过")
        _, d2 = req(r, "GET", f"/batches/{b2['id']}")
        check("中继批次状态 blocked_terminal", d2["status"] == "blocked_terminal")

        # ---- E. 中继重启：队列恢复且成功回调不重复 ----
        print("E. 中继进程重启恢复")
        conn = sqlite3.connect(main_db)
        cb_before = conn.execute(
            "SELECT COUNT(*) FROM relay_callbacks WHERE batch_id=?",
            (b1["id"],)).fetchone()[0]
        conn.close()
        pr.terminate(); pr.wait(timeout=10)
        procs = [("main", pm)]

        # 再导入第三个批次用于重启后继续转发（从实时状态挑仍 scheduled 的批次）
        _, live = req(m, "GET", "/api/state")
        live_scheduled = [x for x in live["batches"] if x["status"] == "scheduled"]
        assert live_scheduled, "车辆故障后应仍有其他批次处于 scheduled"
        b3 = live_scheduled[0]
        _, pkg3 = req(m, "GET", f"/api/execution-package/v1/{b3['id']}")

        pr2 = start(os.path.join(RELAY_BACKEND, "server.py"), {
            "RELAY_PORT": str(RELAY_PORT), "RELAY_DB": relay_db,
            "RELAY_HOST": "127.0.0.1", "RELAY_CALLBACK_ADAPTER": "http",
            "RELAY_CALLBACK_URL": f"http://127.0.0.1:{MAIN_PORT}/api/relay-callback",
            "RELAY_TICK_SECONDS": "0.5", "RELAY_BACKOFF_BASE": "0.5"},
            RELAY_BACKEND, "relay2")
        procs.append(("relay", pr2))
        wait_up(f"http://127.0.0.1:{RELAY_PORT}/relay/api/health")
        r = f"http://127.0.0.1:{RELAY_PORT}/relay/api"

        # 重启后既有数据完整：b1 仍全转发、b2 仍阻塞
        _, d1 = req(r, "GET", f"/batches/{b1['id']}")
        check("重启后 b1 转发计数保持 3", d1["cursor"] == 3)
        _, d2b = req(r, "GET", f"/batches/{b2['id']}")
        check("重启后 b2 仍终态阻塞", d2b["status"] == "blocked_terminal")

        req(r, "POST", "/packages/import", {"package": pkg3})
        req(r, "POST", "/receipts/batch", {"batch_ref": b3["id"], "receipts": [
            {"seq": 1, "receipt_no": "RR-2001", "type": "departed",
             "batch_summary": pkg3["batch_summary"]}]})
        wait_for(lambda: req(r, "GET", f"/batches/{b3['id']}")[1]
                 ["counts"]["forwarded"] == 1, message="重启实例继续转发 b3")

        time.sleep(1.0)  # 留出时间确认没有重复回调
        conn = sqlite3.connect(main_db)
        cb_after = conn.execute(
            "SELECT COUNT(*) FROM relay_callbacks WHERE batch_id=?",
            (b1["id"],)).fetchone()[0]
        conn.close()
        check("b1 已成功回调在重启后零重复", cb_after == cb_before,
              f"before={cb_before} after={cb_after}")

        # ---- F. 终端下载清单 ----
        print("F. 现场终端清单")
        _, man = req(r, "GET", "/terminal/manifest")
        refs = {x["batch_ref"] for x in man["batches"]}
        check("清单包含全部已导入批次", {b1["id"], b2["id"], b3["id"]} <= refs)

    finally:
        for name, p in procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
