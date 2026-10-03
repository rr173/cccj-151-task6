"""中继值班操作的岗位权限与双人复核。

三类岗位权限（db.ROLES）：
  observe         观察：只读查看批次/队列/审计/归档与审批记录
  dispatch        调度：申请/批准 执行包导入、失败重试、终态解除重投
  archive_review  归档审查：申请/批准 批次封存

敏感指令（ACTION_ROLES）不直接执行，而是先形成**待批事项**(approvals)：
  * 请求指纹 request_hash = sha256(canonical{动作, 参数, 申请人})，
    同指纹重复申请返回同一事项（幂等重放）；
  * 资源版本 resource_version = 申请瞬间目标资源状态的 canonical sha256，
    批准时重算：目标状态已变 → 事项失效，拒绝批准；
  * 批准人必须是与申请人**不同的账号**，且批准时双方权限仍有效；
  * 并发批准用 compare-and-swap（UPDATE ... WHERE status='pending'）保证只有一次生效；
  * 重复批准返回第一次结果（幂等）；
  * 批准后在同一把锁、同一事务语义内调用原有业务函数，**不绕过任何业务校验**：
    业务拒绝（不可封存/冲突/摘要不符…）→ 事项 execute_failed 并回传原错误；
  * 账号停用、申请人岗位调整、目标资源变化、超时 → 未决事项立即失效（invalid），
    已生效动作不回滚；
  * 全部过程写 approval_decisions（只追加），重启后从 SQLite 完整恢复。
"""
import json
import os

from core import RelayError, canonical, now, sha256_hex
import db as dbmod


# ============================================================ 账号与权限

def list_accounts(db):
    rows = db.conn.execute(
        "SELECT username,display_name,roles,active,created_at,updated_at,note"
        " FROM accounts ORDER BY username").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["role_list"] = [x for x in (r["roles"] or "").split(",") if x]
        d["role_labels"] = [dbmod.ROLE_LABELS.get(x, x) for x in d["role_list"]]
        out.append(d)
    return out


def _normalize_roles(roles):
    if isinstance(roles, str):
        roles = [x.strip() for x in roles.split(",")]
    if not isinstance(roles, (list, tuple)):
        raise RelayError(400, "malformed", "roles 必须是数组")
    out, seen = [], set()
    for x in roles:
        x = str(x).strip()
        if not x:
            continue
        if x not in dbmod.ROLES:
            raise RelayError(400, "malformed",
                             f"未知权限 {x}（支持 {', '.join(dbmod.ROLES)}）")
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def get_account(db, username):
    if not username:
        return None
    return db.conn.execute("SELECT * FROM accounts WHERE username=?",
                           (str(username),)).fetchone()


def require_account(db, username):
    a = get_account(db, username)
    if not a:
        raise RelayError(401, "unknown_account", f"账号 {username or '?'} 不存在或未登录")
    if not a["active"]:
        raise RelayError(403, "account_disabled", f"账号 {username} 已停用")
    return a


def account_roles(row):
    return set(x for x in (row["roles"] or "").split(",") if x)


def require_role(row, role):
    """账号有效且具备指定岗位权限。"""
    if not row["active"]:
        raise RelayError(403, "account_disabled", f"账号 {row['username']} 已停用")
    if role not in account_roles(row):
        raise RelayError(
            403, "forbidden_role",
            f"该操作需要「{dbmod.ROLE_LABELS.get(role, role)}」权限，"
            f"账号 {row['username']} 当前权限："
            f"{('、'.join(dbmod.ROLE_LABELS.get(x, x) for x in account_roles(row))) or '无'}")


def upsert_account(db, username, display_name=None, roles=None, active=None, note=None,
                   actor="admin"):
    """创建或调整账号；岗位调整会立即失效该账号申请中的、失去对应权限的未决事项。"""
    username = str(username or "").strip()
    if not username:
        raise RelayError(400, "malformed", "缺少 username")
    existing = get_account(db, username)
    t = now()
    if existing:
        new_roles = (_normalize_roles(roles) if roles is not None
                     else sorted(account_roles(existing)))
        new_active = bool(existing["active"]) if active is None else bool(active)
        new_name = existing["display_name"] if display_name is None \
            else str(display_name)
        new_note = existing["note"] if note is None else str(note)
        db.conn.execute(
            "UPDATE accounts SET display_name=?,roles=?,active=?,updated_at=?,note=?"
            " WHERE username=?",
            (new_name, ",".join(new_roles), int(new_active), t, new_note, username))
        # 立即失效：停用 → 该账号全部未决申请；权限收缩 → 失去所需权限的未决申请
        invalidated = _invalidate_for_account(
            db, username, new_roles, disabled=not new_active)
        db.audit("account_updated", actor=actor,
                 detail={"username": username, "roles": new_roles, "active": new_active,
                         "invalidated_approvals": invalidated})
        db.mark_revision()
        db.commit()
        return {"username": username, "created": False, "roles": new_roles,
                "active": new_active, "invalidated": invalidated}

    new_roles = _normalize_roles(roles if roles is not None else [])
    new_active = True if active is None else bool(active)
    db.conn.execute(
        "INSERT INTO accounts(username,display_name,roles,active,created_at,"
        "updated_at,note) VALUES(?,?,?,?,?,?,?)",
        (username, str(display_name or username), ",".join(new_roles),
         int(new_active), t, t, str(note or "")))
    db.audit("account_created", actor=actor,
             detail={"username": username, "roles": new_roles, "active": new_active})
    db.commit()
    return {"username": username, "created": True, "roles": new_roles,
            "active": new_active, "invalidated": []}


def set_active(db, username, active, actor="admin"):
    a = get_account(db, username)
    if not a:
        raise RelayError(404, "not_found", f"账号 {username} 不存在")
    return upsert_account(db, username, active=bool(active), actor=actor)


def _invalidate_for_account(db, username, current_roles, disabled=False):
    """账号停用/权限收缩时，把其名下未决事项立即置为失效（已生效动作不回滚）。"""
    invalidated = []
    cur_roles = set(current_roles)
    for r in db.conn.execute(
            "SELECT * FROM approvals WHERE status='pending' AND requested_by=?",
            (username,)).fetchall():
        need = dbmod.ACTION_ROLES.get(r["action"])
        if disabled or (need and need not in cur_roles):
            reason = "account_disabled" if disabled else "role_changed"
            _mark_invalid(db, r, reason,
                          {"username": username, "roles": sorted(cur_roles)})
            invalidated.append(r["id"])
    return invalidated


# ============================================================ 资源版本

def _pkg_snapshot(db, ref):
    """执行包维度状态：是否存在/内容哈希/封存标记（与回执活动无关）。"""
    p = db.conn.execute(
        "SELECT content_hash,archived FROM packages WHERE batch_ref=?",
        (ref,)).fetchone()
    if not p:
        return {"exists": False}
    return {"exists": True, "content_hash": p["content_hash"],
            "archived": bool(p["archived"])}


def _batch_snapshot(db, ref):
    """批次维度状态：游标 + 每条回执(序号/状态/结果码) + 封存标记。

    任何会改变目标指令执行前提的状态变化（转发推进/退避/终态/封存）都会改变版本，
    从而使引用该批次的未决事项失效。
    """
    p = db.conn.execute(
        "SELECT content_hash,archived FROM packages WHERE batch_ref=?",
        (ref,)).fetchone()
    snap = {"exists": bool(p),
            "archived": bool(p["archived"]) if p else False,
            "cursor": None, "receipts": []}
    cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                          (ref,)).fetchone()
    if cur:
        snap["cursor"] = cur["next_seq"]
    for r in db.conn.execute(
            "SELECT seq,state,result_code FROM receipts WHERE batch_ref=? ORDER BY seq",
            (ref,)).fetchall():
        snap["receipts"].append([r["seq"], r["state"], r["result_code"]])
    return snap


def resource_version(db, action, payload):
    """计算申请/批准时刻的目标资源版本（canonical sha256）。"""
    if action == "package_import":
        ref = str((payload or {}).get("batch_ref", "")
                  or (payload or {}).get("package", {}).get("batch_ref", ""))
        snap = _pkg_snapshot(db, ref)
    elif action in ("manual_retry", "ack_terminal", "batch_seal"):
        ref = str((payload or {}).get("batch_ref", ""))
        snap = _batch_snapshot(db, ref)
    else:
        raise RelayError(400, "malformed", f"未知敏感动作 {action}")
    return sha256_hex(canonical({"action": action, "state": snap}))


# ============================================================ 待批事项

def ttl_seconds():
    return float(os.environ.get("RELAY_APPROVAL_TTL", str(dbmod.DEFAULT_APPROVAL_TTL)))


def _fingerprint(action, params, username):
    return sha256_hex(canonical(
        {"action": action, "params": params, "requested_by": username}))


def _extract_params(action, body):
    """从请求体抽取指令参数（note 不参与指纹）。返回 (params, exec_payload, note)。"""
    note = str(body.get("note", "") or "")
    if action == "package_import":
        pkg = body.get("package")
        if not isinstance(pkg, dict):
            raise RelayError(400, "malformed", "缺少 package 对象")
        params = {"package": pkg, "source": str(body.get("source", "approval"))}
        return params, {"package": pkg, "source": params["source"]}, note
    if action in ("manual_retry", "ack_terminal", "batch_seal"):
        ref = str(body.get("batch_ref", "") or "").strip()
        if not ref:
            raise RelayError(400, "malformed", "缺少 batch_ref")
        if action == "manual_retry":
            seq = body.get("seq")
            if seq is not None:
                try:
                    seq = int(seq)
                except (TypeError, ValueError):
                    raise RelayError(400, "malformed", "seq 必须是整数")
            params = {"batch_ref": ref, "seq": seq}
            return params, {**params, "note": note}, note
        if action == "batch_seal":
            params = {"batch_ref": ref}
            return params, {**params, "note": note}, note
        # ack_terminal 必须指定 seq
        try:
            seq = int(body["seq"])
        except (KeyError, TypeError, ValueError):
            raise RelayError(400, "malformed", "缺少整数 seq")
        params = {"batch_ref": ref, "seq": seq}
        return params, {**params, "note": note}, note
    raise RelayError(400, "malformed",
                     f"未知敏感动作 {action}（支持 {', '.join(dbmod.ACTION_ROLES)}）")


def _new_id(db, fp):
    n = int(db.get_meta("approval_seq", "0") or 0) + 1
    db.set_meta("approval_seq", str(n))
    return f"AP-{n:05d}-{fp[:8]}"


def request_approval(db, body, username):
    """提交敏感指令申请：形成带资源版本与请求指纹的待批事项。"""
    action = str(body.get("action", "") or "").strip()
    if action not in dbmod.ACTION_ROLES:
        raise RelayError(400, "malformed",
                         f"未知敏感动作 {action}（支持 {', '.join(dbmod.ACTION_ROLES)}）")
    account = require_account(db, username)
    need = dbmod.ACTION_ROLES[action]
    require_role(account, need)

    params, exec_payload, note = _extract_params(action, body)
    fp = _fingerprint(action, params, username)

    # ---- 幂等：同指纹重复申请 ----
    #   * 原事项仍 pending/approved（未决或批准中）→ 返回原事项（防重复申请）；
    #   * 原事项已 executed/rejected（成功或已拒）→ 返回原事项（重复操作不二次执行）；
    #   * 原事项已 invalid/execute_failed（失效或业务校验失败）→ 允许重提新事项，
    #     旧事项保留完整决策记录。
    existing = db.conn.execute("SELECT * FROM approvals WHERE request_hash=?",
                               (fp,)).fetchall()
    if existing:
        last = existing[-1]
        if last["status"] not in ("invalid", "execute_failed"):
            return _view(db, last, viewer=username,
                         replay=True,
                         message="同指纹申请已存在，返回原事项（幂等重放）")

    ver = resource_version(db, action, params)
    t = now()
    ap_id = _new_id(db, fp)
    expires = t + ttl_seconds()
    ref = str(params.get("batch_ref", "")
              or params.get("package", {}).get("batch_ref", ""))
    db.conn.execute(
        "INSERT INTO approvals(id,action,request_hash,resource_ref,resource_version,"
        "payload,requested_by,requested_at,status,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ap_id, action, fp, ref, ver,
         json.dumps(exec_payload, ensure_ascii=False), username, t,
         "pending", expires))
    db.approval_created(ap_id)
    db.decision(ap_id, "requested", actor=username, action=action,
                detail={"params": params, "resource_version": ver,
                        "resource_ref": ref, "note": note,
                        "expires_at": expires})
    db.audit("approval_requested", ref, actor=username,
             detail={"approval_id": ap_id, "action": action,
                     "resource_version": ver})
    db.commit()
    row = db.conn.execute("SELECT * FROM approvals WHERE id=?", (ap_id,)).fetchone()
    return _view(db, row, viewer=username, replay=False,
                 message="待批事项已创建，等待另一账号复核")


def _mark_invalid(db, row, reason, detail=None):
    """把未决事项置为失效并追加决策记录（调用方负责提交）。"""
    t = now()
    db.conn.execute(
        "UPDATE approvals SET status='invalid',invalid_reason=?,decided_at=? WHERE id=?",
        (reason, t, row["id"]))
    db.decision(row["id"], "invalidated", actor="system", action=row["action"],
                detail={"reason": reason, **(detail or {})})
    db.audit("approval_invalidated", row["resource_ref"], actor="system",
             detail={"approval_id": row["id"], "action": row["action"],
                     "reason": reason})


def expire_pending(db, sweep_approved=False):
    """超时失效（启动/每秒巡检/读取时调用）；返回失效数量。

    sweep_approved=True（仅启动时）额外兜底崩溃在「已批准未执行」窗口的事项：
    approved 正常只会瞬间存在（批准后同请求内立即执行业务）；若进程恰在该窗口
    崩溃，重启后无法确认业务是否执行，置为失效(interrupted)，由值班人员核对后
    重新申请——绝不自动补执行。
    """
    t = now()
    rows = db.conn.execute(
        "SELECT * FROM approvals WHERE status='pending' AND expires_at<?",
        (t,)).fetchall()
    for r in rows:
        _mark_invalid(db, r, "timeout", {"expires_at": r["expires_at"], "ts": t})
    stuck = []
    if sweep_approved:
        stuck = db.conn.execute(
            "SELECT * FROM approvals WHERE status='approved'").fetchall()
        for r in stuck:
            _mark_invalid(db, r, "interrupted",
                          {"note": "进程在批准后、执行完成前退出；请核对后重新申请"})
    if rows or stuck:
        db.commit()
    return len(rows) + len(stuck)


def invalidate_on_precommit(db):
    """提交前回调：凡资源版本已变化的未决事项立即失效（事务内新建者除外）。"""
    rows = db.conn.execute(
        "SELECT * FROM approvals WHERE status='pending'").fetchall()
    for r in rows:
        if r["id"] in db._approvals_created_tx:
            continue
        if r["id"] in getattr(db, "_approvals_executing", set()):
            continue
        try:
            current = resource_version(db, r["action"], json.loads(r["payload"]))
        except RelayError:
            continue
        if current != r["resource_version"]:
            _mark_invalid(db, r, "resource_changed",
                          {"version_at_request": r["resource_version"],
                           "version_now": current})


# ============================================================ 批准 / 拒绝

def _get_pending(db, ap_id):
    r = db.conn.execute("SELECT * FROM approvals WHERE id=?", (ap_id,)).fetchone()
    if not r:
        raise RelayError(404, "not_found", f"待批事项 {ap_id} 不存在")
    return r


def _stored_outcome(db, row):
    """已终局事项的幂等回放结果。"""
    result = json.loads(row["execution_result"]) if row["execution_result"] else None
    return _view_by_row(db, row, viewer=None, replay=True, result=result,
                        message="该事项已有决策，返回第一次结果（幂等）")


def decide_approval(db, ap_id, approver_name, decision, note=""):
    """批准(approve)/拒绝(reject) 待批事项。

    批准前置（任一不满足即拒）：事项仍 pending；批准人是有效账号且具备所需岗位；
    批准人≠申请人；申请人权限仍有效；未超时；资源版本未变化。
    通过后调用原业务函数执行业务（不绕过业务校验），CAS 更新保证并发只有一次生效。
    """
    if decision not in ("approve", "reject"):
        raise RelayError(400, "malformed", "decision 必须是 approve/reject")
    approver = require_account(db, approver_name)
    row = _get_pending(db, ap_id)

    # 已失效（超时/资源变化/停用/角色调整）→ 409，带失效原因；不得再批准
    if row["status"] == "invalid":
        raise RelayError(409, "approval_invalidated",
                         f"待批事项已失效（{row['invalid_reason']}），不能再批准",
                         {"reason": row["invalid_reason"], "approval_id": ap_id})

    # 已批准/拒绝/执行终局 → 幂等回放第一次结果（重复批准保持幂等，不二次执行）
    if row["status"] != "pending":
        return _stored_outcome(db, row)

    need = dbmod.ACTION_ROLES.get(row["action"])
    require_role(approver, need)

    # 申请人不能自批
    if row["requested_by"] == approver_name:
        raise RelayError(403, "self_approval_forbidden",
                         "申请人不能批准自己发起的事项，必须由另一账号复核")

    # 超时（顺手把事项置失效）
    if row["expires_at"] <= now():
        _mark_invalid(db, row, "timeout", {"expires_at": row["expires_at"]})
        db.commit()
        raise RelayError(409, "approval_expired",
                         "待批事项已超时失效", {"approval_id": ap_id})

    # 申请人账号/权限仍有效
    requester = get_account(db, row["requested_by"])
    if not requester or not requester["active"]:
        _mark_invalid(db, row, "account_disabled",
                      {"username": row["requested_by"]})
        db.commit()
        raise RelayError(409, "approval_invalidated",
                         f"申请人账号 {row['requested_by']} 已停用，事项失效",
                         {"reason": "account_disabled"})
    if need not in account_roles(requester):
        _mark_invalid(db, row, "role_changed",
                      {"username": row["requested_by"],
                       "roles": sorted(account_roles(requester))})
        db.commit()
        raise RelayError(409, "approval_invalidated",
                         f"申请人 {row['requested_by']} 已不再具备所需权限，事项失效",
                         {"reason": "role_changed"})

    # 目标状态未变化
    current_ver = resource_version(db, row["action"], json.loads(row["payload"]))
    if current_ver != row["resource_version"]:
        _mark_invalid(db, row, "resource_changed",
                      {"version_at_request": row["resource_version"],
                       "version_now": current_ver})
        db.commit()
        raise RelayError(409, "approval_invalidated",
                         "目标资源状态自申请后已变化，事项失效",
                         {"reason": "resource_changed",
                          "version_at_request": row["resource_version"],
                          "version_now": current_ver})

    t = now()
    if decision == "reject":
        cur = db.conn.execute(
            "UPDATE approvals SET status='rejected',decided_by=?,decided_at=? "
            "WHERE id=? AND status='pending'", (approver_name, t, ap_id))
        if cur.rowcount == 0:
            db.conn.rollback()
            fresh = _get_pending(db, ap_id)
            return _stored_outcome(db, fresh)
        db.decision(ap_id, "rejected", actor=approver_name, action=row["action"],
                    detail={"note": note})
        db.audit("approval_rejected", row["resource_ref"], actor=approver_name,
                 detail={"approval_id": ap_id, "action": row["action"],
                         "requested_by": row["requested_by"], "note": note})
        db.commit()
        fresh = _get_pending(db, ap_id)
        return _view_by_row(db, fresh, viewer=approver_name, replay=False,
                            result=None, message="事项已拒绝")

    # ---- 批准：先 CAS 占位（并发只有一次生效），再执行业务 ----
    cur = db.conn.execute(
        "UPDATE approvals SET status='approved',decided_by=?,decided_at=? "
        "WHERE id=? AND status='pending'", (approver_name, t, ap_id))
    if cur.rowcount == 0:
        db.conn.rollback()
        fresh = _get_pending(db, ap_id)
        return _stored_outcome(db, fresh)
    db.decision(ap_id, "approved", actor=approver_name, action=row["action"],
                detail={"note": note, "requested_by": row["requested_by"]})
    db.audit("approval_approved", row["resource_ref"], actor=approver_name,
             detail={"approval_id": ap_id, "action": row["action"],
                     "requested_by": row["requested_by"]})

    # 先把「已批准」决策落盘提交：即便后续业务函数内部回滚/崩溃，
    # 批准事实与 CAS 占位都不丢失（业务副作用由各业务函数自身的事务保证一致性）。
    db.commit()

    # 业务函数内部会自行提交（含资源版本推进）；执行期间不得把本事项当作
    # 「目标状态已变化的未决事项」失效——本事项的批准正是这次变化的原因。
    db._approvals_executing.add(ap_id)
    actor_label = f"{row['requested_by']}(复核:{approver_name})"
    try:
        result = _execute(db, row["action"], json.loads(row["payload"]), actor_label)
    except RelayError as e:
        # 原有业务校验拒绝：事项记 execute_failed（批准留痕不回滚），错误原样返回
        db.conn.execute(
            "UPDATE approvals SET status='execute_failed',invalid_reason='',"
            "execution_result=?,executed_at=? WHERE id=?",
            (json.dumps({"error": e.message, "code": e.code,
                         "status": e.status, "extra": e.extra}, ensure_ascii=False),
             now(), ap_id))
        db.decision(ap_id, "execute_failed", actor="system", action=row["action"],
                    detail={"code": e.code, "message": e.message, "extra": e.extra})
        db.audit("approval_execute_failed", row["resource_ref"],
                 actor=actor_label,
                 detail={"approval_id": ap_id, "code": e.code, "message": e.message})
        db.commit()
        # 业务错误透传给调用方（保留原状态码/错误码，不绕过校验）
        e.extra = {**e.extra, "approval_id": ap_id, "approval_status": "execute_failed"}
        raise

    db.conn.execute(
        "UPDATE approvals SET status='executed',execution_result=?,executed_at=? "
        "WHERE id=?", (json.dumps(result, ensure_ascii=False), now(), ap_id))
    db.decision(ap_id, "executed", actor=actor_label, action=row["action"],
                detail={"result": result})
    db.audit("approval_executed", row["resource_ref"], actor=actor_label,
             detail={"approval_id": ap_id, "action": row["action"]})
    db.commit()
    fresh = _get_pending(db, ap_id)
    return _view_by_row(db, fresh, viewer=approver_name, replay=False, result=result,
                        message="复核通过，指令已执行")


def _execute(db, action, payload, actor):
    """在锁内调用原业务函数（与直接调用同一套校验与事务）。"""
    import archive
    import core
    if action == "package_import":
        return core.import_package(
            db, payload["package"], actor=actor,
            source=str(payload.get("source") or "approval"))
    if action == "manual_retry":
        return core.manual_retry(
            db, str(payload["batch_ref"]), seq=payload.get("seq"),
            actor=actor, note=str(payload.get("note", "")))
    if action == "ack_terminal":
        return core.acknowledge_and_retry(
            db, str(payload["batch_ref"]), int(payload["seq"]),
            actor=actor, note=str(payload.get("note", "")))
    if action == "batch_seal":
        return archive.seal_batch(
            db, str(payload["batch_ref"]), actor=actor,
            note=str(payload.get("note", "")))
    raise RelayError(400, "malformed", f"未知敏感动作 {action}")


# ============================================================ 视图

def _row_dict(r):
    d = dict(r)
    d["action_label"] = dbmod.ACTION_LABELS.get(r["action"], r["action"])
    d["payload_obj"] = json.loads(r["payload"])
    if d.get("execution_result"):
        try:
            d["execution_result_obj"] = json.loads(d["execution_result"])
        except ValueError:
            d["execution_result_obj"] = None
    return d


def _can_decide(db, row, viewer):
    if row["status"] != "pending" or not viewer:
        return False
    va = get_account(db, viewer)
    if not va or not va["active"]:
        return False
    need = dbmod.ACTION_ROLES.get(row["action"])
    return need in account_roles(va) and row["requested_by"] != viewer


def _view_by_row(db, row, viewer=None, replay=False, result=None, message=""):
    d = _row_dict(row)
    d["can_approve"] = False
    d["can_reject"] = False
    if viewer is not None:
        d["can_approve"] = _can_decide(db, row, viewer)
        d["can_reject"] = d["can_approve"]
    d["idempotent_replay"] = replay
    d["execution_result_obj"] = result if result is not None else d.get(
        "execution_result_obj")
    d["message"] = message
    return d


def _view(db, row, viewer=None, **kw):
    return _view_by_row(db, row, viewer=viewer, **kw)


def list_approvals(db, viewer=None, status=None, requested_by=None, limit=200):
    sql = "SELECT * FROM approvals"
    cond, params = [], []
    if status:
        if isinstance(status, str):
            status = [status]
        cond.append("status IN (" + ",".join("?" * len(status)) + ")")
        params.extend(status)
    if requested_by:
        cond.append("requested_by=?")
        params.append(requested_by)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY requested_at DESC LIMIT ?"
    params.append(limit)
    return [_view_by_row(db, r, viewer=viewer)
            for r in db.conn.execute(sql, params).fetchall()]


def todo_for(db, username):
    """本人待办：① 我申请且仍未决的；② 待我（有权限且非本人）复核的。"""
    account = get_account(db, username)
    roles = account_roles(account) if account else set()
    mine, awaiting = [], []
    for r in db.conn.execute(
            "SELECT * FROM approvals WHERE status='pending'"
            " ORDER BY requested_at").fetchall():
        view = _view_by_row(db, r, viewer=username)
        if r["requested_by"] == username:
            mine.append(view)
        elif dbmod.ACTION_ROLES.get(r["action"]) in roles:
            awaiting.append(view)
    return {"username": username, "my_requests": mine,
            "awaiting_my_review": awaiting,
            "total": len(mine) + len(awaiting)}


def decisions(db, approval_id=None, limit=300):
    sql = ("SELECT d.*, a.requested_by FROM approval_decisions d"
           " JOIN approvals a ON a.id=d.approval_id")
    params = []
    if approval_id:
        sql += " WHERE d.approval_id=?"
        params.append(approval_id)
    sql += " ORDER BY d.id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in db.conn.execute(sql, params).fetchall():
        d = dict(r)
        try:
            d["detail_obj"] = json.loads(d["detail"])
        except ValueError:
            d["detail_obj"] = None
        out.append(d)
    return out


# ============================================================ 默认账号

DEFAULT_ACCOUNTS = [
    ("zhang_obs", "张伟（观察员）", ["observe"]),
    ("li_disp", "李娜（调度）", ["observe", "dispatch"]),
    ("wang_disp", "王强（调度）", ["observe", "dispatch"]),
    ("zhao_arch", "赵敏（归档审查）", ["observe", "archive_review"]),
    ("sun_arch", "孙磊（归档审查）", ["observe", "archive_review"]),
]


def seed_default_accounts(db):
    """首次启动播种演示账号（幂等；已存在的账号不覆盖）。"""
    if get_account(db, DEFAULT_ACCOUNTS[0][0]):
        return False
    t = now()
    for username, name, roles in DEFAULT_ACCOUNTS:
        db.conn.execute(
            "INSERT INTO accounts(username,display_name,roles,active,created_at,"
            "updated_at,note) VALUES(?,?,?,?,?,?,?)",
            (username, name, ",".join(roles), 1, t, t, "默认演示账号"))
    db.commit()
    return True
