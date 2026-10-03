"""主系统侧的中继对接面（出站执行包 + 回调接口）。

两个只读/只追加端点，不改变编排系统的既有规划语义：

  GET  /api/execution-package/v1/{batch_id}
      导出指定已发布批次的不可变执行包（供离线中继存档与现场终端下载）。
      包内含批次摘要、车辆、目的地、路径、搭载家庭与每个家庭明细；
      batch_ref = 主系统批次 id，batch_summary 由关键字段拼装，
      车辆/目的地若日后变化，回调校验即可识别。

  POST /api/relay-callback
      接收中继按序转发的车队回执（departed/failed_to_depart/arrived）。
      * 按 idempotency_key 幂等：中继超时重试、重启重发都返回首次响应；
      * 批次已撤回（cancelled）→ {"result":"rejected","reason":"batch_withdrawn"}；
      * 回执携带的车辆/目的地与当前批次不一致 →
        vehicle_changed / destination_changed（终态拒绝）；
      * 合法回执 → {"result":"ack"}，并记入审计；
      * departed 类回执若批次仍 scheduled，按既有 depart 语义落"离场承诺"。
"""
import json
import time


class RelayLinkError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _household_map(d):
    return {r["ref"]: dict(r)
            for r in d.conn.execute("SELECT * FROM households")}


def build_execution_package(d, batch_id):
    b = d.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not b:
        raise RelayLinkError(404, "not_found", f"批次 {batch_id} 不存在")
    vehicle = d.conn.execute("SELECT * FROM vehicles WHERE code=?",
                             (b["vehicle_code"],)).fetchone()
    shelter = d.conn.execute("SELECT * FROM shelters WHERE code=?",
                             (b["shelter_code"],)).fetchone()
    refs = [r["household_ref"] for r in d.conn.execute(
        "SELECT household_ref FROM assignments WHERE batch_id=? ORDER BY id",
        (batch_id,))]
    households = _household_map(d)
    items = []
    for ref in refs:
        hh = households.get(ref)
        items.append({
            "household_ref": ref,
            "name": hh["name"] if hh else "",
            "members": hh["members"] if hh else 0,
            "mobility": hh["mobility"] if hh else 0,
            "pets": hh["pets"] if hh else 0,
            "contact": hh["contact"] if hh else "",
        })
    route = json.loads(b["route"])
    summary = (f"波次{b['wave']} · 车辆{b['vehicle_code']} · "
               f"{b['grid']} → {b['shelter_code']} · {len(items)}户")
    package = {
        "schema": "evac.execution-package/v1",
        "batch_ref": b["id"],
        "package_id": b["order_id"],
        "batch_summary": summary,
        "seq_base": 1,
        "issued_at": b["departed_at"] or time.time(),
        "wave": b["wave"],
        "origin": {"code": b["grid"]},
        "vehicle": ({"code": vehicle["code"], "name": vehicle["name"],
                     "seats": vehicle["seats"],
                     "accessible": bool(vehicle["accessible"]),
                     "status": vehicle["status"]} if vehicle else
                    {"code": b["vehicle_code"]}),
        "destination": ({"code": shelter["code"], "name": shelter["name"],
                         "status": shelter["status"]} if shelter else
                        {"code": b["shelter_code"]}),
        "route": route,
        "items": items,
        "batch_status": b["status"],
    }
    _snapshot_export(d, b, vehicle, shelter)
    return package


def _snapshot_export(d, b, vehicle, shelter):
    """首次导出即固化车辆/目的地快照（已存在则不动）。回调对照快照判变化。"""
    exists = d.conn.execute(
        "SELECT 1 FROM execution_exports WHERE batch_id=?", (b["id"],)).fetchone()
    if exists:
        return
    d.conn.execute(
        "INSERT INTO execution_exports(batch_id,vehicle_code,shelter_code,"
        "vehicle_status,shelter_status,exported_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(batch_id) DO NOTHING",
        (b["id"], b["vehicle_code"], b["shelter_code"],
         vehicle["status"] if vehicle else "",
         shelter["status"] if shelter else "", time.time()))
    d.log_event("execution_package_exported",
                json.dumps({"batch_id": b["id"],
                            "vehicle": b["vehicle_code"],
                            "shelter": b["shelter_code"]}, ensure_ascii=False))
    # 导出虽是 GET，但快照必须持久，回调才能识别车辆/目的地变化
    d.commit()


def list_execution_batches(d, status=None):
    sql = "SELECT id,order_id,seq,wave,vehicle_code,grid,shelter_code,status FROM batches"
    params = ()
    if status:
        sql += " WHERE status=?"
        params = (status,)
    sql += " ORDER BY order_id,seq"
    return [dict(r) for r in d.conn.execute(sql, params)]


# ---------------------------------------------------------------- 回调

RECEIPT_TYPES = ("departed", "failed_to_depart", "arrived")


def _first_response(d, key):
    r = d.conn.execute(
        "SELECT response_json FROM relay_callbacks WHERE idempotency_key=?",
        (key,)).fetchone()
    return json.loads(r["response_json"]) if r else None


def receive_relay_callback(d, body):
    """在调用方持有 db.lock 时执行。返回 (status_code, response_obj, replay)。"""
    key = str(body.get("idempotency_key") or "").strip()
    if not key:
        raise RelayLinkError(400, "malformed", "缺少 idempotency_key")

    replay = _first_response(d, key)
    if replay is not None:
        replay["idempotent_replay"] = True
        return 200, replay, True

    batch_ref = str(body.get("batch_ref") or "").strip()
    rtype = str(body.get("type") or body.get("receipt_type") or "").strip()
    if rtype not in RECEIPT_TYPES:
        raise RelayLinkError(400, "malformed", f"未知回执类型 {rtype}")
    b = d.conn.execute("SELECT * FROM batches WHERE id=?", (batch_ref,)).fetchone()
    if not b:
        resp = {"result": "rejected", "reason": "batch_withdrawn",
                "message": f"批次 {batch_ref} 不存在（视为已撤回/失效）"}
        _persist_callback(d, body, key, resp)
        d.log_event("relay_callback_rejected",
                    json.dumps({"batch_ref": batch_ref, "reason": "batch_withdrawn",
                                "key": key}, ensure_ascii=False))
        d.commit()
        return 200, resp, False

    # 终态条件 1：批次已撤回（事件驱动重算只撤回未出发批次）
    if b["status"] == "cancelled":
        resp = {"result": "rejected", "reason": "batch_withdrawn",
                "message": f"批次 {batch_ref} 已撤回，等待重新编排；回执不得入账"}
        _persist_callback(d, body, key, resp)
        d.log_event("relay_callback_rejected",
                    json.dumps({"batch_ref": batch_ref, "reason": "batch_withdrawn",
                                "seq": body.get("seq"), "receipt_no": body.get("receipt_no")},
                               ensure_ascii=False))
        d.commit()
        return 200, resp, False

    # 终态条件 2/3：车辆或目的地相对"导出快照"已变化
    snap = d.conn.execute("SELECT * FROM execution_exports WHERE batch_id=?",
                          (batch_ref,)).fetchone()
    base_vehicle = snap["vehicle_code"] if snap else b["vehicle_code"]
    base_shelter = snap["shelter_code"] if snap else b["shelter_code"]
    veh_now = d.conn.execute("SELECT code,status FROM vehicles WHERE code=?",
                             (b["vehicle_code"],)).fetchone()
    if base_vehicle != b["vehicle_code"]:
        return _terminal(d, body, key, "vehicle_changed", batch_ref,
                         f"执行车辆已从 {base_vehicle} 变为 {b['vehicle_code']}")
    if veh_now and veh_now["status"] == "down":
        return _terminal(d, body, key, "vehicle_changed", batch_ref,
                         f"执行车辆 {b['vehicle_code']} 已故障下线")

    if base_shelter != b["shelter_code"]:
        return _terminal(d, body, key, "destination_changed", batch_ref,
                         f"目的地已从 {base_shelter} 变为 {b['shelter_code']}")
    shelter = d.conn.execute("SELECT status FROM shelters WHERE code=?",
                             (b["shelter_code"],)).fetchone()
    if shelter and shelter["status"] == "suspended":
        return _terminal(d, body, key, "destination_changed", batch_ref,
                         f"目的地避难点 {b['shelter_code']} 已暂停接收")

    # —— 合法回执 ——
    resp = {"result": "ack", "message": f"{rtype} 回执已受理",
            "batch_id": batch_ref, "seq": body.get("seq"),
            "receipt_no": body.get("receipt_no")}
    _persist_callback(d, body, key, resp)

    # 业务联动：已离场回执在批次仍 scheduled 时形成承诺（复用既有离场语义）
    committed = False
    if rtype == "departed" and b["status"] == "scheduled":
        try:
            from server import depart_batch  # 延迟导入避免循环依赖
            depart_batch(d, batch_ref, {"commander": "relay"})
            committed = True
        except Exception as e:  # noqa: BLE001 —— 离场失败不影响回执 ack 幂等留痕
            resp["warning"] = f"离场承诺联动失败：{e}"
    d.log_event("relay_callback_ack",
                json.dumps({"batch_ref": batch_ref, "seq": body.get("seq"),
                            "receipt_no": body.get("receipt_no"), "type": rtype,
                            "commitment_applied": committed, "key": key},
                           ensure_ascii=False))
    d.commit()
    resp["commitment_applied"] = committed
    return 200, resp, False


def _terminal(d, body, key, reason, batch_ref, message):
    resp = {"result": "rejected", "reason": reason, "message": message}
    _persist_callback(d, body, key, resp)
    d.log_event("relay_callback_rejected",
                json.dumps({"batch_ref": batch_ref, "reason": reason,
                            "seq": body.get("seq"), "receipt_no": body.get("receipt_no")},
                           ensure_ascii=False))
    d.commit()
    return 200, resp, False


def _persist_callback(d, body, key, resp):
    d.conn.execute(
        "INSERT INTO relay_callbacks(idempotency_key,batch_id,seq,receipt_no,type,"
        "response_json,ts) VALUES(?,?,?,?,?,?,?)",
        (key, str(body.get("batch_ref", "")), body.get("seq"),
         str(body.get("receipt_no", "")),
         str(body.get("type") or body.get("receipt_type") or ""),
         json.dumps(resp, ensure_ascii=False), time.time()))
