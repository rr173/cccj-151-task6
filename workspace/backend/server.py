"""山火应急撤离编排系统 —— HTTP 服务（Python 标准库，零第三方依赖）。

线程模型：ThreadingHTTPServer 接收请求，所有"读-改-写"事务在 DB 全局锁内串行；
配合数据修订号(revision)实现乐观并发控制：
  * 方案制定时记录 base_revision；
  * 发布时若修订号已变（他人已发布、道路/车辆/避难点/火线变化），拒绝发布；
  * 多名指挥员基于同一旧方案同时发布，只有第一份生效。

推演分支：/api/branches/{id}/... 下的全部操作都作用于分支的**独立 SQLite 文件**，
与实战库物理隔离；提升(promote)是唯一把推演资源改动合并回实战库的通道，
合并采用 base/实战/推演 三方字段级冲突检查，任一冲突整体拒绝。
"""
import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from db import DB
from planner import Snapshot, plan as run_plan, validate_plan
from seed import seed
import branches as branches_mod
import relay_link

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")
DB_PATH = os.environ.get("EVAC_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "..", "data", "evac.db"))

db = DB(DB_PATH)
if os.environ.get("EVAC_NO_SEED") != "1" and db.revision() == 0 and not db.get_meta("seeded"):
    seed(db)
    db.set_meta("seeded", "1")


# ---------------------------------------------------------------- 通用工具

def now_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def rows(d, table):
    return [dict(r) for r in d.conn.execute(f"SELECT * FROM {table}")]


def release_scheduled_batches(d, where_sql, params=(), reason=""):
    """取消未出发的计划批次并释放其预占资源（已出发批次永不动）。

    返回被释放的家庭 ref 列表。
    """
    cur = d.conn.execute(
        f"SELECT id FROM batches WHERE status='scheduled' AND {where_sql}", params)
    batch_ids = [r["id"] for r in cur]
    released = []
    for bid in batch_ids:
        hs = [r["household_ref"] for r in d.conn.execute(
            "SELECT household_ref FROM assignments WHERE batch_id=?", (bid,))]
        released.extend(hs)
        d.conn.execute("DELETE FROM assignments WHERE batch_id=?", (bid,))
        d.conn.execute("UPDATE batches SET status='cancelled' WHERE id=?", (bid,))
    if batch_ids:
        d.log_event("batch_cancelled",
                    json.dumps({"batches": batch_ids, "released": released,
                                "reason": reason}, ensure_ascii=False))
    return released


# ---------------------------------------------------------------- 状态视图

def build_state(d):
    c = d.conn
    grids = {r["code"]: dict(r) for r in c.execute("SELECT * FROM grids")}
    shelters = {r["code"]: dict(r) for r in c.execute("SELECT * FROM shelters")}
    vehicles = {r["code"]: dict(r) for r in c.execute("SELECT * FROM vehicles")}
    roads = rows(d, "roads")
    households = {r["ref"]: dict(r) for r in c.execute("SELECT * FROM households")}

    # 批次 + 安置
    batch_rows = c.execute(
        "SELECT * FROM batches ORDER BY order_id, seq").fetchall()
    assignment_rows = c.execute("SELECT * FROM assignments").fetchall()
    hh_by_batch = {}
    for a in assignment_rows:
        hh_by_batch.setdefault(a["batch_id"], []).append(a["household_ref"])

    committed_beds = {code: 0 for code in shelters}
    reserved_beds = {code: 0 for code in shelters}
    hh_state = {ref: {"status": "unplanned"} for ref in households}

    batches_out = []
    for b in batch_rows:
        refs = hh_by_batch.get(b["id"], [])
        status = b["status"]
        for ref in refs:
            if status == "departed":
                hh_state[ref] = {
                    "status": "departed", "batch": b["id"],
                    "shelter": b["shelter_code"], "wave": b["wave"]}
                committed_beds[b["shelter_code"]] += households[ref]["members"]
            elif status == "scheduled":
                hh_state[ref] = {
                    "status": "scheduled", "batch": b["id"],
                    "shelter": b["shelter_code"], "wave": b["wave"]}
                reserved_beds[b["shelter_code"]] += households[ref]["members"]
            else:
                hh_state.setdefault(ref, {"status": "unplanned"})
        batches_out.append({
            "id": b["id"], "order_id": b["order_id"], "seq": b["seq"],
            "wave": b["wave"], "vehicle": b["vehicle_code"],
            "grid": b["grid"], "shelter": b["shelter_code"],
            "route": json.loads(b["route"]), "status": status,
            "departed_at": b["departed_at"],
            "households": refs,
        })

    orders = [dict(r) for r in c.execute(
        "SELECT id,plan_id,commander,revision,created_at FROM orders ORDER BY created_at")]

    capacity = {}
    for code, s in shelters.items():
        capacity[code] = {
            "name": s["name"], "beds": s["beds"],
            "committed": committed_beds[code],
            "reserved": reserved_beds[code],
            "remaining": s["beds"] - committed_beds[code] - reserved_beds[code],
            "accessible": bool(s["accessible"]),
            "pets_allowed": bool(s["pets_allowed"]),
            "status": s["status"],
        }

    events = [dict(r) for r in c.execute(
        "SELECT id,ts,kind,detail,revision FROM event_log ORDER BY id DESC LIMIT 50")]

    pending = [ref for ref, st in hh_state.items()
               if st["status"] == "unplanned"
               and grids.get(households[ref]["grid"], {}).get("threatened")]

    return {
        "revision": d.revision(),
        "grids": list(grids.values()),
        "shelters": [dict(r) for r in c.execute("SELECT * FROM shelters")],
        "roads": roads,
        "vehicles": [dict(r) for r in c.execute("SELECT * FROM vehicles")],
        "households": list(households.values()),
        "household_state": hh_state,
        "batches": batches_out,
        "orders": orders,
        "capacity": capacity,
        "pending_households": pending,
        "events": list(reversed(events)),
    }


# ---------------------------------------------------------------- 事务操作

def import_entities(d, kind, items):
    """通用导入：按主键 upsert，重复导入不产生重复数据。"""
    if not isinstance(items, list):
        raise ValueError("载荷必须是数组")
    specs = {
        "grids": ("grids", ["code", "name", "x", "y"],
                  "INSERT INTO grids(code,name,x,y,threatened) VALUES(?,?,?,?,1) "
                  "ON CONFLICT(code) DO UPDATE SET name=excluded.name,x=excluded.x,y=excluded.y",
                  lambda it: (it["code"], it["name"], it["x"], it["y"])),
        "shelters": ("shelters", ["code", "name", "x", "y", "beds"],
                     "INSERT INTO shelters(code,name,x,y,beds,accessible,pets_allowed,status)"
                     " VALUES(?,?,?,?,?,?,?,'active') ON CONFLICT(code) DO UPDATE SET"
                     " name=excluded.name,x=excluded.x,y=excluded.y,beds=excluded.beds,"
                     " accessible=excluded.accessible,pets_allowed=excluded.pets_allowed",
                     lambda it: (it["code"], it["name"], it["x"], it["y"],
                                 int(it["beds"]), int(it.get("accessible", 0)),
                                 int(it.get("pets_allowed", 1)))),
        "roads": ("roads", ["code", "a", "b"],
                  "INSERT INTO roads(code,a,b,length,capacity,status)"
                  " VALUES(?,?,?,?,?,'open') ON CONFLICT(code) DO UPDATE SET a=excluded.a,"
                  "b=excluded.b,length=excluded.length,capacity=excluded.capacity",
                  lambda it: (it["code"], it["a"], it["b"],
                              float(it.get("length", 1)),
                              int(it.get("capacity", 1)))),
        "vehicles": ("vehicles", ["code", "name", "seats"],
                     "INSERT INTO vehicles(code,name,seats,accessible,status)"
                     " VALUES(?,?,?,?,'available') ON CONFLICT(code) DO UPDATE SET"
                     " name=excluded.name,seats=excluded.seats,accessible=excluded.accessible",
                     lambda it: (it["code"], it["name"], int(it["seats"]),
                                 int(it.get("accessible", 0)))),
    }
    if kind not in specs:
        raise ValueError(f"未知导入类型 {kind}")
    table, required, sql, conv = specs[kind]
    payloads = []
    for it in items:
        for f in required:
            if f not in it:
                raise ValueError(f"{kind} 记录缺少字段 {f}: {it}")
        payloads.append(conv(it))
    d.conn.executemany(sql, payloads)
    rev = d.bump_revision()
    d.log_event("import", json.dumps({"kind": kind, "count": len(items)},
                                     ensure_ascii=False))
    d.commit()
    return {"imported": len(items), "revision": rev}


def register_households(d, items):
    """家庭登记：按外部户号 ref 去重（INSERT OR IGNORE），重复导入不重复登记。"""
    if not isinstance(items, list):
        items = [items]
    inserted, skipped = 0, 0
    for it in items:
        for f in ("ref", "grid", "members"):
            if f not in it:
                raise ValueError(f"家庭记录缺少字段 {f}")
        exists = d.conn.execute("SELECT 1 FROM households WHERE ref=?",
                                (it["ref"],)).fetchone()
        if exists:
            skipped += 1
            continue
        d.conn.execute(
            "INSERT INTO households(ref,name,grid,members,mobility,pets,contact)"
            " VALUES(?,?,?,?,?,?,?)",
            (it["ref"], it.get("name", ""), it["grid"], int(it["members"]),
             int(it.get("mobility", 0)), int(it.get("pets", 0)),
             it.get("contact", "")))
        inserted += 1
    rev = d.bump_revision()
    d.log_event("households_register",
                json.dumps({"inserted": inserted, "skipped": skipped},
                           ensure_ascii=False))
    d.commit()
    return {"inserted": inserted, "skipped_duplicates": skipped, "revision": rev}


def create_plan(d, body):
    """计算候选方案（只包含受威胁网格中的未出发家庭）。不改动任何现有命令。"""
    snap = Snapshot(d)
    result = run_plan(snap)
    pid = now_id("plan")
    d.conn.execute(
        "INSERT INTO plans(id,name,commander,base_revision,status,payload,created_at)"
        " VALUES(?,?,?,?, 'candidate', ?,?)",
        (pid, body.get("name", "候选方案"), body.get("commander", ""),
         d.revision(), json.dumps(result, ensure_ascii=False), time.time()))
    d.commit()
    result["id"] = pid
    result["revision"] = d.revision()
    return result


def list_plans(d):
    out = []
    for r in d.conn.execute(
            "SELECT id,name,commander,base_revision,status,created_at FROM plans"
            " ORDER BY created_at DESC").fetchall():
        dd = dict(r)
        payload = json.loads(d.conn.execute(
            "SELECT payload FROM plans WHERE id=?", (dd["id"],)).fetchone()["payload"])
        dd["summary"] = {
            "feasible": payload["feasible"], "max_wave": payload["max_wave"],
            "totals": payload["totals"],
            "unassigned_count": len(payload["unassigned"]),
            "batch_count": len(payload["batches"]),
        }
        out.append(dd)
    return out


def get_plan_payload(d, pid):
    r = d.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not r:
        raise NotFound("方案不存在")
    return r, json.loads(r["payload"])


def publish_plan(d, pid, body):
    """发布撤离令。

    乐观并发：候选方案的 base_revision 必须等于当前修订号，否则说明方案所依据的
    资源/路况已变化（或已有他人发布），返回 409，指挥员需基于最新方案重新比较发布。
    发布后：旧的"未出发"批次全部取消替换；"已出发"批次与床位占用原样保留。
    """
    row = d.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
    if not row:
        raise NotFound("方案不存在")
    if row["status"] == "published":
        raise Conflict("该方案已经发布，不能重复生效")
    payload = json.loads(row["payload"])
    if not payload["feasible"]:
        raise Conflict("方案含无法安置的家庭，不能发布；请先恢复资源或调整约束")
    if row["base_revision"] != d.revision():
        raise Conflict(
            f"方案已过期：制定于修订号 {row['base_revision']}，当前为 {d.revision()}，"
            "已有其他指挥员发布或资源状态发生变化，请基于最新方案重新发布")
    if not payload["batches"]:
        raise Conflict("方案为空（当前没有待撤离家庭）")

    # 发布前独立复核全部硬约束（基于当前最新快照，防止方案制定后资源已变）
    violations = validate_plan(Snapshot(d), payload["batches"])
    if violations:
        raise Conflict("方案违反硬约束，禁止发布：" + "；".join(violations[:8])
                       + ("……" if len(violations) > 8 else ""))

    commander = body.get("commander", row["commander"])

    # 替换所有未出发批次（已出发批次保持承诺不变）
    release_scheduled_batches(d, "1=1", reason=f"发布新方案 {pid}")
    d.conn.execute("UPDATE plans SET status='superseded' WHERE status='published'")
    d.conn.execute("UPDATE plans SET status='published' WHERE id=?", (pid,))

    oid = now_id("order")
    d.conn.execute(
        "INSERT INTO orders(id,plan_id,commander,revision,created_at) VALUES(?,?,?,?,?)",
        (oid, pid, commander, d.revision(), time.time()))
    for b in payload["batches"]:
        bid = now_id("batch")
        d.conn.execute(
            "INSERT INTO batches(id,order_id,seq,wave,vehicle_code,grid,shelter_code,"
            "route,status) VALUES(?,?,?,?,?,?,?,?, 'scheduled')",
            (bid, oid, b["seq"], b["wave"], b["vehicle"], b["grid"], b["shelter"],
             json.dumps({"edges": b["route_edges"], "nodes": b["route_nodes"]})))
        for h in b["households"]:
            d.conn.execute(
                "INSERT INTO assignments(batch_id,household_ref) VALUES(?,?)",
                (bid, h["ref"]))
    rev = d.bump_revision()
    d.log_event("order_published",
                json.dumps({"order": oid, "plan": pid, "commander": commander,
                            "batches": len(payload["batches"])}, ensure_ascii=False))
    d.commit()
    return {"order_id": oid, "plan_id": pid, "revision": rev,
            "batches": len(payload["batches"])}


def depart_batch(d, bid, body):
    """车辆离场：批次与目的地床位占用即视为已承诺，后续重算不得再触碰。"""
    b = d.conn.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
    if not b:
        raise NotFound("批次不存在")
    if b["status"] != "scheduled":
        raise Conflict(f"批次当前状态为 {b['status']}，无法标记离场")

    ts = time.time()
    d.conn.execute(
        "UPDATE batches SET status='departed', departed_at=? WHERE id=?", (ts, bid))
    d.conn.execute("UPDATE vehicles SET status='departed' WHERE code=?",
                   (b["vehicle_code"],))
    # 同一辆车后续的未出发波次已无法执行 → 取消并释放，等待重新编排
    released = release_scheduled_batches(
        d, "vehicle_code=? AND id<>?", (b["vehicle_code"], bid),
        reason=f"车辆 {b['vehicle_code']} 已离场")
    rev = d.bump_revision()
    d.log_event("batch_departed",
                json.dumps({"batch": bid, "vehicle": b["vehicle_code"],
                            "shelter": b["shelter_code"], "released": released},
                           ensure_ascii=False))
    d.commit()
    return {"batch": bid, "status": "departed", "released_for_replan": released,
            "revision": rev}


def apply_event(d, body):
    """火线扩大 / 道路封闭与恢复 / 车辆故障与恢复 / 避难点暂停与恢复。

    任何事件都只影响未出发批次；已出发承诺不变。
    """
    kind = body.get("type", "")
    released = []

    def get(table, code):
        r = d.conn.execute(f"SELECT * FROM {table} WHERE code=?", (code,)).fetchone()
        if not r:
            raise NotFound(f"{table} 中不存在 {code}")
        return dict(r)

    if kind == "road_close":
        get("roads", body["code"])
        d.conn.execute("UPDATE roads SET status='closed' WHERE code=?", (body["code"],))
        released = _cancel_batches_on_road(d, body["code"],
                                           f"道路封闭 {body['code']}")
    elif kind == "road_open":
        get("roads", body["code"])
        d.conn.execute("UPDATE roads SET status='open' WHERE code=?", (body["code"],))
    elif kind == "vehicle_down":
        get("vehicles", body["code"])
        d.conn.execute("UPDATE vehicles SET status='down' WHERE code=?", (body["code"],))
        released = release_scheduled_batches(
            d, "vehicle_code=?", (body["code"],), reason=f"车辆故障 {body['code']}")
    elif kind == "vehicle_recovered":
        get("vehicles", body["code"])
        d.conn.execute(
            "UPDATE vehicles SET status='available' WHERE code=? AND status<>'departed'",
            (body["code"],))
    elif kind == "shelter_suspend":
        get("shelters", body["code"])
        d.conn.execute("UPDATE shelters SET status='suspended' WHERE code=?",
                       (body["code"],))
        released = release_scheduled_batches(
            d, "shelter_code=?", (body["code"],),
            reason=f"避难点暂停接收 {body['code']}")
    elif kind == "shelter_resume":
        get("shelters", body["code"])
        d.conn.execute("UPDATE shelters SET status='active' WHERE code=?",
                       (body["code"],))
    elif kind == "fire_expand":
        get("grids", body["code"])
        d.conn.execute("UPDATE grids SET threatened=1 WHERE code=?", (body["code"],))
        if body.get("close_roads"):
            for rc in body["close_roads"]:
                d.conn.execute("UPDATE roads SET status='closed' WHERE code=?", (rc,))
                released += _cancel_batches_on_road(d, rc, f"火线扩大封路 {rc}")
    elif kind == "fire_clear":
        get("grids", body["code"])
        d.conn.execute("UPDATE grids SET threatened=0 WHERE code=?", (body["code"],))
    else:
        raise ValueError(f"未知事件类型 {kind}")

    rev = d.bump_revision()
    d.log_event(kind, json.dumps({"code": body.get("code"),
                                  "released": released}, ensure_ascii=False))
    d.commit()
    return {"type": kind, "revision": rev, "released_for_replan": released}


def _cancel_batches_on_road(d, road_code, reason):
    cur = d.conn.execute("SELECT id, route FROM batches WHERE status='scheduled'")
    ids = [r["id"] for r in cur if road_code in json.loads(r["route"]).get("edges", [])]
    released = []
    for bid in ids:
        hs = [r["household_ref"] for r in d.conn.execute(
            "SELECT household_ref FROM assignments WHERE batch_id=?", (bid,))]
        released.extend(hs)
        d.conn.execute("DELETE FROM assignments WHERE batch_id=?", (bid,))
        d.conn.execute("UPDATE batches SET status='cancelled' WHERE id=?", (bid,))
    if ids:
        d.log_event("batch_cancelled",
                    json.dumps({"batches": ids, "released": released,
                                "reason": reason}, ensure_ascii=False))
    return released


# ---------------------------------------------------------------- 异常

class HttpError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class NotFound(HttpError):
    def __init__(self, m): super().__init__(404, m)


class Conflict(HttpError):
    def __init__(self, m): super().__init__(409, m)


# ---------------------------------------------------------------- 分支请求分发

def _audit_sim(bd, bid, action, body, result):
    bd.log_event("sim_" + action,
                 json.dumps({"request": body, "result_revision":
                             result.get("revision") if isinstance(result, dict) else None},
                            ensure_ascii=False))


def branch_get(branch_id, sub, main_db):
    """分支上下文的只读接口（已废弃分支仍可只读查看其最终状态与差异）。"""
    br = branches_mod.get_branch(main_db, branch_id)
    if br["status"] == "active":
        _, bd = branches_mod.open_branch_db(main_db, branch_id)
    else:
        bd = branches_mod.DB(br["db_file"])
    if sub in ("", "state", None):
        st = build_state(bd)
        st["context"] = {"kind": "branch", "id": br["id"], "name": br["name"],
                         "status": br["status"],
                         "base_live_revision": br["base_live_revision"],
                         "baseline_revision": br["baseline_revision"]}
        return st
    if sub == "plans":
        return list_plans(bd)
    if sub.startswith("plans/"):
        pid = sub.split("/", 1)[1]
        r = bd.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
        if not r:
            raise NotFound("方案不存在")
        dd = dict(r)
        dd["payload"] = json.loads(dd["payload"])
        return dd
    if sub == "events":
        return [dict(x) for x in bd.conn.execute(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT 100")]
    if sub == "diff":
        return branches_mod.build_diff(main_db, branch_id)
    if sub == "audit":
        return branches_mod.list_audit(main_db, branch_id)
    raise NotFound("未知分支接口")


def branch_post(branch_id, sub, body, main_db):
    """分支上下文的写接口（全部落在分支独立库，带 op_id 幂等）。"""
    body = body or {}

    # 废弃是生命周期操作：分支已废弃时仍需幂等应答，不能被"已废弃"守卫拦截
    if sub == "discard":
        return branches_mod.discard_branch(main_db, branch_id, body)
    # 提升自带状态检查与拒绝逻辑（含审计）
    if sub == "promote":
        return branches_mod.promote_branch(main_db, branch_id, body)

    br, bd = branches_mod.open_branch_db(main_db, branch_id)

    # 以下为分支内模拟写操作，op_id 幂等
    op_id = body.get("op_id") or body.get("idem_key")
    if op_id:
        cached = bd.conn.execute(
            "SELECT response FROM sim_ops WHERE scope=? AND op_id=?",
            (branch_id, op_id)).fetchone()
        if cached:
            out = dict(json.loads(cached["response"]))
            out["idempotent_replay"] = True
            return out

    if sub == "events":
        result = apply_event(bd, body)
    elif sub == "plan":
        result = create_plan(bd, body)
    elif sub.startswith("plans/") and sub.endswith("/publish"):
        pid = sub.split("/")[1]
        result = publish_plan(bd, pid, body)
    elif sub.startswith("batches/") and sub.endswith("/depart"):
        bid = sub.split("/")[1]
        result = depart_batch(bd, bid, body)
    else:
        raise NotFound("未知分支接口")

    bd.conn.execute(
        "INSERT OR IGNORE INTO sim_ops(scope,op_id,response,ts) VALUES(?,?,?,?)",
        (branch_id, op_id or f"auto_{uuid.uuid4().hex[:10]}",
         json.dumps(result, ensure_ascii=False), time.time()))
    bd.commit()
    main_db.conn.execute(
        "INSERT INTO branch_audit(ts,branch_id,action,detail,op_id) VALUES(?,?,?,?,?)",
        (time.time(), branch_id, "simulate",
         json.dumps({"sub": sub, "body": body, "revision": result.get("revision")},
                    ensure_ascii=False), op_id))
    main_db.commit()
    result["context"] = {"kind": "branch", "id": branch_id, "name": br["name"],
                         "branch_revision": result.get("revision")}
    return result


# ---------------------------------------------------------------- HTTP Handler

class Handler(BaseHTTPRequestHandler):
    server_version = "EvacOrchestrator/1.0"

    def log_message(self, fmt, *args):
        pass  # 安静运行；需要调试可打开下行
        # super().log_message(fmt, *args)

    def _json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise HttpError(400, "请求体不是合法 JSON")

    def _static(self, path):
        if path == "/" or path == "":
            path = "index.html"
        safe = os.path.normpath(os.path.join(FRONTEND_DIR, path.lstrip("/")))
        if not safe.startswith(os.path.abspath(FRONTEND_DIR)) or not os.path.isfile(safe):
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
            os.path.splitext(safe)[1], "application/octet-stream")
        with open(safe, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            with db.lock:
                # ---- 推演分支只读接口 ----
                if p.startswith("/api/branches/"):
                    parts = p.strip("/").split("/")  # ['api','branches',bid, sub...]
                    bid = parts[2]
                    sub = "/".join(parts[3:])
                    return self._branch_response(bid, sub, None, method="GET")
                if p == "/api/branches":
                    return self._json(200, branches_mod.list_branches(db))

                if p == "/api/state":
                    return self._json(200, build_state(db))
                if p == "/api/plans":
                    return self._json(200, list_plans(db))
                if p.startswith("/api/plans/"):
                    pid = p.rsplit("/", 1)[-1]
                    r = db.conn.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
                    if not r:
                        raise NotFound("方案不存在")
                    dd = dict(r)
                    dd["payload"] = json.loads(dd["payload"])
                    return self._json(200, dd)
                if p == "/api/events":
                    return self._json(200, [dict(x) for x in db.conn.execute(
                        "SELECT * FROM event_log ORDER BY id DESC LIMIT 100")])

                # ---- 出站执行包（供离线中继导入/现场下载）----
                if p == "/api/execution-packages":
                    from urllib.parse import parse_qs
                    qs = parse_qs(u.query)
                    status = qs.get("status", [None])[0]
                    return self._json(200, {
                        "batches": relay_link.list_execution_batches(db, status)})
                if p.startswith("/api/execution-package/v1/"):
                    bid = p.rsplit("/", 1)[-1]
                    return self._json(200,
                                      relay_link.build_execution_package(db, bid))
            if p.startswith("/api/"):
                raise NotFound("未知接口")
            return self._static(p)
        except branches_mod.BranchError as e:
            self._json(e.status, {"error": str(e), "code": e.code, **e.extra})
        except relay_link.RelayLinkError as e:
            self._json(e.status, {"error": e.message, "code": e.code})
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"服务器内部错误: {e}"})

    def _branch_response(self, bid, sub, body, method):
        """统一处理分支调用中的锁：分支库与主库锁串行获取（分支操作可能提升回主库）。"""
        with db.lock:
            try:
                if method == "GET":
                    return self._json(200, branch_get(bid, sub, db))
                return self._json(200, branch_post(bid, sub, body, db))
            except branches_mod.BranchError as e:
                self._json(e.status, {"error": str(e), "code": e.code, **e.extra})

    def do_POST(self):
        u = urlparse(self.path)
        p, body = u.path, None
        try:
            body = self._read_body()
            with db.lock:

                # ---- 分支管理 ----
                if p == "/api/branches":
                    return self._json(200, branches_mod.create_branch(db, body or {}))
                if p.startswith("/api/branches/"):
                    parts = p.strip("/").split("/")
                    bid = parts[2]
                    sub = "/".join(parts[3:])
                    return self._branch_response(bid, sub, body or {}, method="POST")

                if p == "/api/plan":
                    return self._json(200, create_plan(db, body or {}))
                if p == "/api/events":
                    return self._json(200, apply_event(db, body or {}))
                if p.startswith("/api/import/"):
                    kind = p.rsplit("/", 1)[-1]
                    return self._json(200, import_entities(db, kind, body.get("items", body)
                                                           if isinstance(body, dict)
                                                           else body))
                if p == "/api/households":
                    items = body.get("items", body) if isinstance(body, dict) else body
                    return self._json(200, register_households(db, items))
                if p.startswith("/api/plans/") and p.endswith("/publish"):
                    pid = p.split("/")[3]
                    return self._json(200, publish_plan(db, pid, body or {}))
                if p.startswith("/api/batches/") and p.endswith("/depart"):
                    bid = p.split("/")[3]
                    return self._json(200, depart_batch(db, bid, body or {}))
                if p == "/api/relay-callback":
                    status_code, resp, _replay = relay_link.receive_relay_callback(
                        db, body or {})
                    return self._json(status_code, resp)
                if p == "/api/reset":
                    seed(db, reset=True)
                    # 清理已删除元数据的孤立分支数据库文件
                    bdir = os.path.join(os.path.dirname(os.path.abspath(db.path)), "branches")
                    if os.path.isdir(bdir):
                        for f in os.listdir(bdir):
                            if f.endswith(".db") or f.endswith(".db-wal") or f.endswith(".db-shm"):
                                try:
                                    os.remove(os.path.join(bdir, f))
                                except OSError:
                                    pass
                    branches_mod._open_branch_dbs.clear()
                    db.set_meta("seeded", "1")
                    db.bump_revision()
                    db.log_event("reset", "{}")
                    db.commit()
                    return self._json(200, {"ok": True, "revision": db.revision()})
            raise NotFound("未知接口")
        except branches_mod.BranchError as e:
            self._json(e.status, {"error": str(e), "code": e.code, **e.extra})
        except relay_link.RelayLinkError as e:
            self._json(e.status, {"error": e.message, "code": e.code})
        except HttpError as e:
            self._json(e.status, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"服务器内部错误: {e}"})


def main():
    host = os.environ.get("EVAC_HOST", "0.0.0.0")
    port = int(os.environ.get("EVAC_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"山火应急撤离编排系统已启动: http://{host}:{port}")
    print(f"数据库: {os.path.abspath(DB_PATH)}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
