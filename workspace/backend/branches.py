"""推演分支（隔离沙盒）核心。

隔离模型
  * 每个分支是一个**独立 SQLite 文件**，由创建时刻的实战库完整备份（backup API）而来；
    分支内的事件、方案、发布、离场全部只写分支文件，实战库物理上不受任何影响。
  * 实战库的 branches / branch_audit / sim_ops 表只保存分支元数据、审计与幂等凭证。

三方合并（提升）
  * 创建时固化"创建基线"资源指纹 base_fingerprint（永不变更），三方检查始终以它为
    merge-base：这样实战在分支创建后新增的承诺（如车辆真实离场）绝不会被分支的
    "旧世界值"回滚。
  * baseline_revision 仅记录最近一次成功提升到达的实战修订号（展示用）。
  * 对每个被跟踪字段比较 base / live / branch：
      base==live==branch        无变化
      base!=branch, base==live  干净改动，可应用
      base!=live, branch==base  仅实战变化，分支未改，不应用
      base!=live, branch!=live  冲突（两边都改且结果不同），整体拒绝
  * 调用方必须携带 expected_live_revision（即做差异比对时看到的实战修订号）；
    与当前实战修订号不一致 → 基线过期，整体拒绝。
  * 推演产生的命令、批次、离场记录、已承诺床位/车辆占用**永不迁移**。
    车辆在分支内被置为 departed（由推演离场产生）属承诺，不作为资源改动提升，
    该字段标记 skipped_commitment，忽略其与实战的任何差异。
"""
import json
import os
import sqlite3
import time

from db import DB

# 允许在分支内修改并在提升时参与三方合并的资源字段
TRACKED = {
    "roads": ("status", "capacity"),
    "shelters": ("status", "beds"),
    "vehicles": ("status",),
    "grids": ("threatened",),
}

TABLE_PK = {t: "code" for t in TRACKED}


class BranchError(Exception):
    """携带 HTTP 状态码的分支错误。"""
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code            # 机器可读错误码：not_found / discarded / stale_baseline / conflict
        self.extra = extra or {}


# ---------------------------------------------------------------- 指纹与资源快照

def _norm(v):
    if isinstance(v, bool):
        return 1 if v else 0
    return v


def resource_fingerprint(d):
    """捕获被跟踪资源字段快照：{table: {code: {field: value}}}。"""
    fp = {}
    for table, fields in TRACKED.items():
        rows = {}
        for r in d.conn.execute(f"SELECT * FROM {table}"):
            rows[r[TABLE_PK[table]]] = {f: _norm(r[f]) for f in fields}
        fp[table] = rows
    return fp


def _branch_dir(main_db_path):
    p = os.path.join(os.path.dirname(os.path.abspath(main_db_path)), "branches")
    os.makedirs(p, exist_ok=True)
    return p


# ---------------------------------------------------------------- 分支库连接（缓存）

_open_branch_dbs = {}   # id -> DB


def branch_db_path(main_db_path, branch_id):
    return os.path.join(_branch_dir(main_db_path), f"{branch_id}.db")


def open_branch_db(main_db, branch_id):
    """返回 (分支元数据 dict, 分支 DB)。分支不存在/已废弃按情况抛错。"""
    br = get_branch(main_db, branch_id)
    if br["status"] != "active":
        raise BranchError(409, "discarded", f"分支 {branch_id} 已废弃，不能再操作")
    bd = _open_branch_dbs.get(branch_id)
    if bd is None:
        bd = DB(br["db_file"])
        _open_branch_dbs[branch_id] = bd
    return br, bd


# ---------------------------------------------------------------- 生命周期

def get_branch(main_db, branch_id):
    r = main_db.conn.execute("SELECT * FROM branches WHERE id=?", (branch_id,)).fetchone()
    if not r:
        raise BranchError(404, "not_found", f"分支不存在：{branch_id}")
    d = dict(r)
    d["base_fingerprint"] = json.loads(d["base_fingerprint"])
    d["baseline_fingerprint"] = json.loads(d["baseline_fingerprint"])
    return d


def list_branches(main_db):
    out = []
    for r in main_db.conn.execute(
            "SELECT id,name,status,base_live_revision,baseline_revision,idem_key,"
            "created_at,discarded_at FROM branches ORDER BY created_at DESC"):
        out.append(dict(r))
    return out


def create_branch(main_db, body):
    """从当前实战态势复制一个隔离分支（幂等：同一 idem_key 返回已存在分支）。"""
    name = (body.get("name") or "推演分支").strip()
    idem_key = body.get("idem_key") or body.get("client_token")
    if idem_key:
        old = main_db.conn.execute(
            "SELECT id FROM branches WHERE idem_key=?", (idem_key,)).fetchone()
        if old:
            br = get_branch(main_db, old["id"])
            return {"id": br["id"], "name": br["name"], "status": br["status"],
                    "base_live_revision": br["base_live_revision"],
                    "idempotent_replay": True}

    import uuid
    bid = f"sim_{uuid.uuid4().hex[:12]}"
    path = branch_db_path(main_db.path, bid)

    # 先在实战事务状态下固化指纹与修订号，再做物理备份
    main_db.commit()
    fp = resource_fingerprint(main_db)
    live_rev = main_db.revision()

    dest = sqlite3.connect(path)
    try:
        main_db.conn.backup(dest)
        dest.commit()
    finally:
        dest.close()

    # 以 DB 重新打开分支文件：自动补齐分支元数据表（sim_ops），并写入分支 meta
    bd = DB(path)
    bd.set_meta("branch_id", bid)
    bd.set_meta("branch_name", name)
    bd.set_meta("base_live_revision", live_rev)
    bd.commit()
    _open_branch_dbs[bid] = bd

    fp_json = json.dumps(fp, ensure_ascii=False)
    main_db.conn.execute(
        "INSERT INTO branches(id,name,status,base_live_revision,base_fingerprint,"
        "baseline_fingerprint,baseline_revision,db_file,idem_key,created_at)"
        " VALUES(?,?,'active',?,?,?,?,?,?,?)",
        (bid, name, live_rev, fp_json, fp_json, live_rev, path, idem_key, time.time()))
    _audit(main_db, bid, "create", {"name": name, "base_live_revision": live_rev,
                                    "idem_key": idem_key})
    main_db.commit()
    return {"id": bid, "name": name, "status": "active",
            "base_live_revision": live_rev, "idempotent_replay": False}


def discard_branch(main_db, branch_id, body=None):
    """废弃分支（幂等：重复废弃返回同一结果）。分支文件保留以备审计/历史查看。"""
    br = get_branch(main_db, branch_id)
    if br["status"] == "discarded":
        return {"id": branch_id, "status": "discarded", "idempotent_replay": True,
                "discarded_at": br["discarded_at"]}
    now = time.time()
    main_db.conn.execute(
        "UPDATE branches SET status='discarded', discarded_at=? WHERE id=?",
        (now, branch_id))
    _audit(main_db, branch_id, "discard", {"at": now})
    main_db.commit()
    _open_branch_dbs.pop(branch_id, None)
    return {"id": branch_id, "status": "discarded", "idempotent_replay": False,
            "discarded_at": now}


# ---------------------------------------------------------------- 幂等操作

def begin_idempotent(main_db, scope, op_id):
    """查找已完成的同 op_id 操作；返回其缓存响应 dict，未找到返回 None。"""
    if not op_id:
        return None
    # 分支作用域的幂等凭证保存在分支库 sim_ops（分支自包含、可随文件恢复）
    target = _open_branch_dbs.get(scope) if scope != "live" else None
    if target is None and scope != "live":
        br = get_branch(main_db, scope)
        target = DB(br["db_file"])
        _open_branch_dbs[scope] = target
    r = target.conn.execute(
        "SELECT response FROM sim_ops WHERE scope=? AND op_id=?",
        (scope, op_id)).fetchone()
    return json.loads(r["response"]) if r else None


def finish_idempotent(main_db, scope, op_id, response, target=None):
    """落库幂等凭证（忽略重复键）。

    模拟操作凭证保存在分支库（随分支文件自包含、可恢复）；
    提升(promote)凭证保存在主库——分支被废弃后仍可审计/重放。
    """
    if not op_id:
        return
    t = target or main_db
    t.conn.execute(
        "INSERT OR IGNORE INTO sim_ops(scope,op_id,response,ts) VALUES(?,?,?,?)",
        (scope, op_id, json.dumps(response, ensure_ascii=False), time.time()))
    t.commit()


def _audit(main_db, branch_id, action, detail, op_id=None):
    main_db.conn.execute(
        "INSERT INTO branch_audit(ts,branch_id,action,detail,op_id) VALUES(?,?,?,?,?)",
        (time.time(), branch_id, action,
         json.dumps(detail, ensure_ascii=False), op_id))


def list_audit(main_db, branch_id, limit=100):
    get_branch(main_db, branch_id)
    return [dict(r) for r in main_db.conn.execute(
        "SELECT id,ts,branch_id,action,detail,op_id FROM branch_audit"
        " WHERE branch_id=? ORDER BY id DESC LIMIT ?", (branch_id, limit))]


# ---------------------------------------------------------------- 三方差异

def _three_way(base, live, br):
    """返回 (changes, conflicts)。
    changes: [{table,code,field,base,value}] 可应用的干净改动
    conflicts: [{table,code,field,base,live,branch}]
    """
    changes, conflicts = [], []
    for table, fields in TRACKED.items():
        codes = set(base.get(table, {})) | set(live.get(table, {})) | set(br.get(table, {}))
        for code in codes:
            bo, lo, ro = (base.get(table, {}).get(code),
                          live.get(table, {}).get(code),
                          br.get(table, {}).get(code))
            if bo is None or ro is None:
                # 对象在基线之后才出现（新增），分支不承载新增能力，跳过
                continue
            if lo is None:
                # 实战删除了对象而分支仍持有 → 视为冲突
                for f in fields:
                    conflicts.append({"table": table, "code": code, "field": f,
                                      "base": bo.get(f), "live": None,
                                      "branch": ro.get(f), "reason": "对象在实战中已不存在"})
                continue
            for f in fields:
                bv, lv, rv = bo.get(f), lo.get(f), ro.get(f)
                if rv == bv:
                    continue  # 分支未改此字段
                if lv == bv:
                    # 仅分支改动 → 干净
                    changes.append({"table": table, "code": code, "field": f,
                                    "base": bv, "value": rv})
                elif lv != rv:
                    # 两边都改且结果不同 → 冲突
                    conflicts.append({"table": table, "code": code, "field": f,
                                      "base": bv, "live": lv, "branch": rv})
                # lv == rv 且 bv 不同：两边改成相同结果，视为无冲突（无需再应用）
    return changes, conflicts


def field_diff(base, live, br):
    """并排视图用：逐字段比较三态，输出带状态标记的差异行。"""
    rows = []
    for table, fields in TRACKED.items():
        codes = set(base.get(table, {})) | set(live.get(table, {})) | set(br.get(table, {}))
        for code in sorted(codes):
            bo = base.get(table, {}).get(code)
            lo = live.get(table, {}).get(code)
            ro = br.get(table, {}).get(code)
            if bo is None:
                continue
            for f in fields:
                bv = bo.get(f) if bo else None
                lv = lo.get(f) if lo else None
                rv = ro.get(f) if ro else None
                if bv == lv == rv:
                    continue
                # 推演内车辆离场产生的承诺占用不参与提升，优先标记
                if table == "vehicles" and f == "status" and rv == "departed" \
                        and bv != "departed":
                    status = "commitment"
                elif lv == rv and bv != lv:
                    status = "same_change"          # 两边改成一致
                elif rv == bv and lv != bv:
                    status = "live_only"            # 仅实战变化
                elif lv == bv and rv != bv:
                    status = "branch_only"          # 仅推演变化（可提升）
                else:
                    status = "conflict"
                rows.append({"table": table, "code": code, "field": f,
                             "base": bv, "live": lv, "branch": rv, "status": status})
    return rows


def promote_branch(main_db, branch_id, body):
    """把推演的资源干净改动提升回实战。任一冲突或过期基线 → 整体拒绝（不写实战）。

    幂等：相同 promote_id 返回首次结果（成功或被拒）。
    """
    body = body or {}
    promote_id = body.get("promote_id")
    if promote_id:
        # 提升凭证存主库：即使分支后来废弃，审计与重放仍可查
        hit = main_db.conn.execute(
            "SELECT response FROM sim_ops WHERE scope=? AND op_id=?",
            (branch_id, promote_id)).fetchone()
        if hit:
            cached = dict(json.loads(hit["response"]))
            cached["idempotent_replay"] = True
            return cached

    br = get_branch(main_db, branch_id)
    if br["status"] != "active":
        raise BranchError(409, "discarded", f"分支 {branch_id} 已废弃，不能提升")

    bd = _open_branch_dbs.get(branch_id)
    if bd is None:
        bd = DB(br["db_file"])
        _open_branch_dbs[branch_id] = bd

    live_rev = main_db.revision()

    # 1) 过期基线：调用方必须显式确认其差异视图所依据的实战修订号
    expected = body.get("expected_live_revision")
    stale = expected is not None and int(expected) != live_rev
    if stale:
        result = {"applied": False, "rejected": True, "reason_code": "stale_baseline",
                  "error": f"基线已过期：差异视图基于实战修订号 {expected}，"
                           f"当前实战修订号为 {live_rev}，请刷新并排差异后重新提升",
                  "expected_live_revision": expected, "current_live_revision": live_rev}
        _audit(main_db, branch_id, "promote_rejected", result, promote_id)
        main_db.commit()
        finish_idempotent(main_db, branch_id, promote_id, result)
        raise BranchError(409, "stale_baseline", result["error"], result)

    # 2) 三方合并：始终以**创建基线**为 merge-base（需求固定语义）。
    #    这样实战在分支创建后新增的承诺（车辆离场等）绝不会因基线推进而被分支的
    #    "旧世界值"回滚；分支只有相对创建基线真正改过的字段才可能被应用。
    base_fp = br["base_fingerprint"]
    live_fp = resource_fingerprint(main_db)
    branch_fp = resource_fingerprint(bd)
    changes, conflicts = _three_way(base_fp, live_fp, branch_fp)

    # 3) 推演离场承诺不作为资源改动（车辆 departed 字段从 changes/conflicts 中剔除）
    skipped_commitments = []
    kept_changes = []
    for ch in changes:
        if (ch["table"] == "vehicles" and ch["field"] == "status"
                and ch["value"] == "departed"
                and base_fp["vehicles"].get(ch["code"], {}).get("status") != "departed"):
            skipped_commitments.append(ch)
        else:
            kept_changes.append(ch)
    conflicts = [c for c in conflicts
                 if not (c["table"] == "vehicles" and c["field"] == "status"
                         and c["branch"] == "departed")]

    if conflicts:
        objs = sorted({f"{c['table']}:{c['code']}" for c in conflicts})
        result = {
            "applied": False, "rejected": True, "reason_code": "conflict",
            "error": "三方冲突检查未通过：实战与推演对同一对象字段做了不同修改，"
                     "已整体拒绝，实战数据未发生任何变化",
            "current_live_revision": live_rev,
            "conflict_objects": objs,
            "conflicts": conflicts,
            "pending_clean_changes": len(kept_changes),
        }
        _audit(main_db, branch_id, "promote_rejected",
               {"reason": "conflict", "objects": objs,
                "fields": [f"{c['table']}.{c['code']}.{c['field']}" for c in conflicts]},
               promote_id)
        main_db.commit()
        finish_idempotent(main_db, branch_id, promote_id, result)
        raise BranchError(409, "conflict", result["error"], result)

    # 4) 原子应用全部干净改动；顺带撤回受影响的未出发批次（与实战事件语义一致）
    applied_fields = []
    released = []
    for ch in kept_changes:
        t, code, f, val = ch["table"], ch["code"], ch["field"], ch["value"]
        main_db.conn.execute(
            f"UPDATE {t} SET {f}=? WHERE code=?", (_norm_val(t, f, val), code))
        applied_fields.append(f"{t}.{code}.{f}={val}")
        if t == "roads" and f == "status" and val == "closed":
            from server import _cancel_batches_on_road
            released += _cancel_batches_on_road(main_db, code,
                                                f"推演提升：道路 {code} 封闭")
        if t == "vehicles" and f == "status" and val == "down":
            from server import release_scheduled_batches
            released += release_scheduled_batches(
                main_db, "vehicle_code=?", (code,),
                reason=f"推演提升：车辆 {code} 故障")
        if t == "shelters" and f == "status" and val == "suspended":
            from server import release_scheduled_batches
            released += release_scheduled_batches(
                main_db, "shelter_code=?", (code,),
                reason=f"推演提升：避难点 {code} 暂停接收")

    rev = main_db.bump_revision()
    main_db.log_event(
        "branch_promoted",
        json.dumps({"branch": branch_id, "fields": applied_fields,
                    "skipped_commitments":
                        [f"{c['table']}.{c['code']}.{c['field']}"
                         for c in skipped_commitments],
                    "released": released}, ensure_ascii=False))

    # 5) 记录上次成功提升的实战修订号（创建基线/创建指纹永不变更，审计留痕）
    main_db.conn.execute(
        "UPDATE branches SET baseline_revision=? WHERE id=?", (rev, branch_id))
    _audit(main_db, branch_id, "promote_ok",
           {"applied_fields": applied_fields, "new_revision": rev,
            "skipped_commitments": [c["code"] for c in skipped_commitments]},
           promote_id)
    main_db.commit()
    # 分支库记录被提升的基线点（审计），但不改动其任何业务数据
    bd.set_meta("last_promoted_live_revision", rev)
    bd.commit()

    result = {"applied": True, "rejected": False, "revision": rev,
              "current_live_revision": rev,
              "applied_fields": applied_fields,
              "applied_count": len(applied_fields),
              "released_for_replan": sorted(set(released)),
              "skipped_commitments":
                  [{"table": c["table"], "code": c["code"], "field": c["field"]}
                   for c in skipped_commitments],
              "note": "仅应用资源字段改动；推演产生的命令、离场记录与已承诺占用未迁移"}
    finish_idempotent(main_db, branch_id, promote_id, result)
    return result


def _norm_val(table, field, val):
    if field == "threatened":
        return 1 if val else 0
    return val


# ---------------------------------------------------------------- 并排差异视图

def _latest_unassigned(d):
    """取该上下文最近一次候选/已发布方案的受阻原因（按户聚合）。"""
    r = d.conn.execute(
        "SELECT payload FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    if not r:
        return {}
    payload = json.loads(r["payload"])
    return {u["ref"]: {"reasons": u["reasons"], "name": u.get("name", ""),
                       "grid": u.get("grid", ""), "members": u.get("members", 0)}
            for u in payload.get("unassigned", [])}


def _shelter_name_map(d):
    return {r["code"]: r["name"] for r in d.conn.execute("SELECT code,name FROM shelters")}


def build_diff(main_db, branch_id):
    """返回实战 vs 推演的并排差异：人员去向、资源余量、受阻原因、字段差异。"""
    from server import build_state
    br = get_branch(main_db, branch_id)
    if br["status"] != "active":
        # 已废弃分支也允许只读查看其最后状态
        bd = DB(br["db_file"])
    else:
        bd = _open_branch_dbs.get(branch_id) or DB(br["db_file"])

    live_state = build_state(main_db)
    branch_state = build_state(bd)

    # 人员去向（以 household_state 为准）
    people = []
    for ref in sorted(live_state["household_state"].keys()
                      | branch_state["household_state"].keys()):
        ls = live_state["household_state"].get(ref, {"status": "absent"})
        bs = branch_state["household_state"].get(ref, {"status": "absent"})
        differs = (ls.get("status"), ls.get("shelter"), ls.get("wave")) != \
                  (bs.get("status"), bs.get("shelter"), bs.get("wave"))
        people.append({"ref": ref, "differs": differs,
                       "live": {"status": ls.get("status"), "shelter": ls.get("shelter"),
                                "wave": ls.get("wave")},
                       "branch": {"status": bs.get("status"), "shelter": bs.get("shelter"),
                                  "wave": bs.get("wave")}})

    # 资源余量（床位）
    shelters = []
    for code in sorted(set(live_state["capacity"]) | set(branch_state["capacity"])):
        lc = live_state["capacity"].get(code)
        bc = branch_state["capacity"].get(code)
        shelters.append({
            "code": code,
            "name": (lc or bc).get("name"),
            "live": None if not lc else {k: lc[k] for k in
                                         ("beds", "committed", "reserved", "remaining", "status")},
            "branch": None if not bc else {k: bc[k] for k in
                                           ("beds", "committed", "reserved", "remaining", "status")},
            "differs": (not lc or not bc) or
                       (lc["committed"], lc["reserved"], lc["remaining"], lc["status"]) !=
                       (bc["committed"], bc["reserved"], bc["remaining"], bc["status"]),
        })

    # 受阻原因
    lu, bu = _latest_unassigned(main_db), _latest_unassigned(bd)
    blockers = []
    for ref in sorted(set(lu) | set(bu)):
        lr = lu.get(ref, {}).get("reasons", [])
        brr = bu.get(ref, {}).get("reasons", [])
        blockers.append({
            "ref": ref,
            "name": (lu.get(ref) or bu.get(ref)).get("name", ""),
            "live_reasons": lr, "branch_reasons": brr,
            "differs": lr != brr,
        })

    base_fp = br["base_fingerprint"]
    live_fp = resource_fingerprint(main_db)
    branch_fp = resource_fingerprint(bd)
    fields = field_diff(base_fp, live_fp, branch_fp)

    return {
        "branch": {"id": br["id"], "name": br["name"], "status": br["status"]},
        "base_live_revision": br["base_live_revision"],
        "last_promoted_revision": br["baseline_revision"],
        "live_revision": live_state["revision"],
        "branch_revision": branch_state["revision"],
        "people": people,
        "shelters": shelters,
        "blockers": blockers,
        "resource_fields": fields,
        "live_summary": {
            "orders": len(live_state["orders"]),
            "departed": sum(1 for b in live_state["batches"] if b["status"] == "departed"),
            "scheduled": sum(1 for b in live_state["batches"] if b["status"] == "scheduled"),
            "pending": len(live_state["pending_households"]),
        },
        "branch_summary": {
            "orders": len(branch_state["orders"]),
            "departed": sum(1 for b in branch_state["batches"] if b["status"] == "departed"),
            "scheduled": sum(1 for b in branch_state["batches"] if b["status"] == "scheduled"),
            "pending": len(branch_state["pending_households"]),
        },
    }
