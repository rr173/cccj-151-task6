"""值班岗位权限与敏感指令双人复核。

敏感指令（执行包导入、失败重试、终态确认重投、批次封存）不立即执行业务，而是先写入
带资源版本与请求指纹的 approval。批准时重新验证：
  * 申请人/批准人账号仍启用且保留所需岗位；
  * 不是申请人自批；
  * 请求未超时；
  * 目标资源状态与创建时快照仍一致；
  * 仍通过原有业务校验。

所有状态均持久化；服务重启后 pending 可继续批准，崩溃在 executing 状态会按幂等业务
安全恢复。请求指纹唯一，因此重复申请/重复批准/并发批准均只能有一次生效。
"""
import hashlib
import json
import os
import time

import archive
import core

ROLE_LABELS = {"observe": "观察", "dispatch": "调度", "archive_review": "归档审查"}
VALID_ROLES = tuple(ROLE_LABELS)
SENSITIVE_ROLE = {
    "package_import": "dispatch",
    "retry": "dispatch",
    "terminal_ack": "dispatch",
    "batch_seal": "archive_review",
}
ACTION_LABELS = {
    "package_import": "执行包导入",
    "retry": "失败重试",
    "terminal_ack": "终态确认重投",
    "batch_seal": "批次封存",
}
PENDING_STATES = ("pending", "executing")


def now():
    return time.time()


def canonical(obj):
    return core.canonical(obj)


def fingerprint_obj(action, body):
    """只纳入业务指令字段；操作者/备注等控制字段不参与幂等。"""
    ignore = {"_actor", "note", "idempotent_replay", "auto_approve"}
    if action == "package_import":
        out = {"package": body.get("package", body), "source": body.get("source", "")}
    else:
        out = {k: v for k, v in body.items() if k not in ignore}
    return {"action": action, "request": out}


def request_fingerprint(action, body):
    return core.sha256_hex(canonical(fingerprint_obj(action, body)))


def ttl_seconds():
    return float(os.environ.get("RELAY_APPROVAL_TTL_SECONDS", "300"))


def _loads(text, default=None):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default if default is not None else {}


# ============================================================ 账号与岗位

def account_view(row):
    return {
        "username": row["username"], "display_name": row["display_name"],
        "roles": _loads(row["roles"], []), "role_labels": [
            ROLE_LABELS.get(x, x) for x in _loads(row["roles"], [])],
        "active": bool(row["active"]), "role_version": row["role_version"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


def get_account(db, username, active_only=False):
    if not username:
        raise core.RelayError(401, "unauthenticated", "缺少值班账号（X-Relay-User）")
    row = db.conn.execute("SELECT * FROM accounts WHERE username=?",
                          (username,)).fetchone()
    if not row or (active_only and not row["active"]):
        raise core.RelayError(403, "account_unavailable",
                              f"账号 {username} 不存在或已停用")
    return row


def require_login(db, username):
    return get_account(db, username, active_only=True)


def require_role(db, username, role):
    acc = require_login(db, username)
    roles = _loads(acc["roles"], [])
    if role not in roles:
        raise core.RelayError(403, "forbidden_role",
                              f"账号 {username} 缺少岗位 {ROLE_LABELS.get(role, role)}",
                              {"required_role": role, "roles": roles})
    return acc


def list_accounts(db, actor=None):
    if actor is not None:
        require_login(db, actor)
    return [account_view(r) for r in db.conn.execute(
        "SELECT * FROM accounts ORDER BY username")]


def require_account_manager(db, username):
    """岗位管理仅用于本地值班台：仍必须是启用账号，且同时拥有三类岗位。"""
    acc = require_login(db, username)
    roles = set(_loads(acc["roles"], []))
    needed = set(VALID_ROLES)
    if not needed.issubset(roles):
        raise core.RelayError(403, "account_manager_required",
                              "账号管理需要同时具备观察、调度、归档审查三类岗位",
                              {"required_roles": list(VALID_ROLES), "roles": sorted(roles)})
    return acc


def upsert_account(db, actor, username, display_name=None, roles=None, active=None):
    require_account_manager(db, actor)
    username = str(username or "").strip()
    if not username:
        raise core.RelayError(400, "malformed", "缺少 username")
    if roles is not None:
        if not isinstance(roles, list) or not roles or any(x not in VALID_ROLES for x in roles):
            raise core.RelayError(400, "malformed",
                                 f"roles 必须是非空数组，取值：{', '.join(VALID_ROLES)}")
    t = now()
    old = db.conn.execute("SELECT * FROM accounts WHERE username=?",
                          (username,)).fetchone()
    old_roles = _loads(old["roles"], []) if old else []
    new_active = int(old["active"]) if old and active is None else int(bool(active))
    new_roles = old_roles if old and roles is None else roles
    if old:
        new_display = old["display_name"] if display_name is None else str(display_name)
    else:
        new_display = str(display_name or username)
    if old:
        version = old["role_version"] + (1 if new_roles != old_roles else 0)
        db.conn.execute(
            "UPDATE accounts SET display_name=?,roles=?,active=?,role_version=?,"
            "updated_at=?,updated_by=? WHERE username=?",
            (new_display, json.dumps(new_roles, ensure_ascii=False), new_active,
             version, t, actor, username))
    else:
        version = 1
        db.conn.execute(
            "INSERT INTO accounts(username,display_name,roles,active,role_version,"
            "created_at,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?)",
            (username, new_display, json.dumps(new_roles, ensure_ascii=False),
             new_active, 1, t, t, actor))
    changed_role = (old and new_roles != old_roles)
    changed_active = (old and int(old["active"]) != int(new_active))
    if changed_role or changed_active:
        invalidate_pending_for_account(
            db, username, "role_changed" if changed_role else "account_disabled",
            actor=actor, commit=False)
    db.audit("account_updated", actor=actor, detail={
        "username": username, "roles": new_roles, "active": bool(new_active),
        "role_version": version, "created": old is None})
    db.commit()
    return account_view(db.conn.execute(
        "SELECT * FROM accounts WHERE username=?", (username,)).fetchone())


def invalidate_pending_for_account(db, username, reason, actor="system", commit=True):
    rows = db.conn.execute(
        "SELECT id,applicant,approver,action,resource_ref FROM approvals"
        " WHERE state='pending' AND (applicant=? OR approver=?)",
        (username, username)).fetchall()
    t = now()
    for r in rows:
        db.conn.execute(
            "UPDATE approvals SET state='invalid',invalid_reason=?,decided_at=? "
            "WHERE id=? AND state='pending'", (reason, t, r["id"]))
        db.audit("approval_invalidated", r["action"], r["resource_ref"],
                 actor=actor, detail={"request_id": r["id"], "reason": reason,
                                      "account": username})
    if commit:
        db.commit()
    return len(rows)


# ============================================================ 资源版本

def _receipt_version(row):
    if not row:
        return None
    return {
        "state": row["state"], "attempts": row["attempts"],
        "last_error": row["last_error"], "result_code": row["result_code"],
        "next_retry_at": row["next_retry_at"], "lease_until": row["lease_until"],
        "updated_at": row["updated_at"],
        "content_hash": row["content_hash"],
    }


def _resource_state(db, action, body, allow_missing=False):
    ref = str(body.get("batch_ref", "")).strip()
    if action == "package_import":
        pkg = body.get("package", body)
        ref = str(pkg.get("batch_ref", "")).strip()
        digest = core.sha256_hex(canonical(pkg))
        row = db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                              (ref,)).fetchone()
        exists = bool(row)
        changed = exists and row["content_hash"] != digest
        if exists and not changed:
            state = "existing_same"
        elif exists and changed:
            state = "existing_different"
        else:
            state = "missing"
        return {"ref": ref, "exists": exists, "changed": changed, "state": state,
                "content_hash": row["content_hash"] if row else None}

    pkg = db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                          (ref,)).fetchone()
    if pkg and pkg["archived"]:
        if action == "batch_seal":
            arch = db.conn.execute(
                "SELECT batch_ref,digest,sealed_at,sealed_by,receipt_count,"
                "audit_count,freed_bytes,manifest FROM archives WHERE batch_ref=?",
                (ref,)).fetchone()
            if arch:
                result = archive._seal_result(arch, replay=True)
                raise _DirectReplay(result)
        raise core.RelayError(409, "batch_archived",
                              f"批次 {ref} 已封存为只读归档，归档记录不再进入转发队列")
    if not pkg:
        if allow_missing:
            return {"ref": ref, "exists": False, "changed": True, "state": "missing"}
        raise core.RelayError(404, "not_found", f"批次 {ref} 不存在")

    if action == "batch_seal":
        reasons = archive.eligibility_reasons(db, pkg)
        cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                              (ref,)).fetchone()
        return {"ref": ref, "exists": True, "archived": bool(pkg["archived"]),
                "package_hash": pkg["content_hash"],
                "cursor": cur["next_seq"] if cur else pkg["seq_base"],
                "ineligible_codes": [x["code"] for x in reasons],
                "state": "sealable" if not reasons else "not_sealable"}

    seq = body.get("seq")
    if action == "terminal_ack":
        seq = int(seq)
        r = db.conn.execute("SELECT * FROM receipts WHERE batch_ref=? AND seq=?",
                            (ref, seq)).fetchone()
        receipt_key = ({"seq": seq, "state": r["state"],
                        "content_hash": r["content_hash"]} if r else None)
        return {"ref": ref, "exists": True, "archived": bool(pkg["archived"]),
                "package_hash": pkg["content_hash"], "seq": seq,
                "receipt": receipt_key,
                "state": r["state"] if r else "missing"}

    # 批准时仍要通过原业务校验：允许后台租约/退避时刻等易失状态变化，
    # 但目标离开可重试状态、封存或执行包改变都会使版本不匹配。
    q = ("SELECT * FROM receipts WHERE batch_ref=? AND state IN "
         "('retry_wait','dispatching') ORDER BY seq")
    params = [ref]
    if seq is not None:
        q = ("SELECT * FROM receipts WHERE batch_ref=? AND seq=? AND state IN "
             "('retry_wait','dispatching') ORDER BY seq")
        params.append(int(seq))
    rows = db.conn.execute(q, params).fetchall()
    receipt_keys = [{"seq": r["seq"], "content_hash": r["content_hash"],
                     "retryable": True}
                    for r in rows]
    return {"ref": ref, "exists": True, "archived": bool(pkg["archived"]),
            "package_hash": pkg["content_hash"], "seq": seq,
            "receipts": receipt_keys,
            "state": "eligible" if rows else "nothing_to_retry"}


def _version_changed(captured, current):
    if not captured or not current:
        return True
    c = dict(captured)
    n = dict(current)
    # 允许只比较决定能否执行的完整状态，不纳入查询时的非关键字段。
    c.pop("changed", None)
    n.pop("changed", None)
    return canonical(c) != canonical(n)


def refresh_approval(db, row, commit=True):
    """动态判定 pending 是否仍有效；一旦失效立即持久化原因。"""
    if row["state"] != "pending":
        return row
    t = now()
    reason = None
    payload = _loads(row["request_payload"])
    applicant = db.conn.execute("SELECT * FROM accounts WHERE username=?",
                                (row["applicant"],)).fetchone()
    if not applicant or not applicant["active"]:
        reason = "applicant_account_disabled"
    elif applicant["role_version"] != row["applicant_role_version"]:
        reason = "applicant_role_changed"
    elif row["required_role"] not in _loads(applicant["roles"], []):
        reason = "applicant_role_lost"
    elif row["expires_at"] <= t:
        reason = "expired"
    else:
        try:
            current = _resource_state(db, row["action"], payload, allow_missing=True)
            if _version_changed(_loads(row["resource_version"]), current):
                reason = "resource_changed"
        except core.RelayError:
            reason = "resource_changed"
    if reason:
        db.conn.execute(
            "UPDATE approvals SET state='invalid',invalid_reason=?,decided_at=?"
            " WHERE id=? AND state='pending'", (reason, t, row["id"]))
        db.audit("approval_invalidated", row["action"], row["resource_ref"],
                 actor=row["applicant"],
                 detail={"request_id": row["id"], "reason": reason})
        if commit:
            db.commit()
        row = db.conn.execute("SELECT * FROM approvals WHERE id=?",
                              (row["id"],)).fetchone()
    return row


# ============================================================ 创建待批事项

def create_request(db, action, body, actor, required_role=None, note=""):
    required_role = required_role or SENSITIVE_ROLE[action]
    acc = require_role(db, actor, required_role)
    body = body if isinstance(body, dict) else {}

    # 执行包重复导入原本就是业务幂等重放，不制造待批事项。
    if action == "package_import":
        pkg = body.get("package", body)
        digest = core.sha256_hex(canonical(pkg))
        same = db.conn.execute(
            "SELECT batch_ref,imported_at FROM packages WHERE content_hash=?",
            (digest,)).fetchone()
        if same:
            res = core.import_package(db, pkg, actor=actor,
                                      source=str(body.get("source", "api")))
            return {"approval_required": False, **res}

    fp = request_fingerprint(action, body)

    # 部分业务幂等重放（如已封存批次的再次封存）必须先于旧待办指纹查询，
    # 否则已经失败/失效的历史同指纹事项会遮蔽当前资源上的原业务幂等结果。
    try:
        version = _precheck(db, action, body)
    except _DirectReplay as e:
        result = e.result
        return {**result, "approval_required": False, "replay": result.get("replay", True)}

    old = db.conn.execute("SELECT * FROM approvals WHERE request_fingerprint=?",
                          (fp,)).fetchone()
    if old and old["state"] in PENDING_STATES + ("rejected", "failed", "invalid", "expired"):
        return {"approval_required": old["state"] in PENDING_STATES,
                "replay": True, "request": approval_view(db, old, decisions=False),
                "result": _loads(old["result_payload"], None),
                "message": "相同请求指纹已存在，返回原待批/决策结果（幂等）"}

    if old:
        old_result = _loads(old["result_payload"], None) or {}
        return {**old_result, "approval_required": False, "replay": True,
                "request": approval_view(db, old, decisions=False),
                "result": old_result,
                "message": "相同请求指纹已存在，返回原待批/决策结果（幂等）"}

    t = now()
    rid = "ap_" + hashlib.sha256((fp + ":" + str(t)).encode("utf-8")).hexdigest()[:24]
    roles = _loads(acc["roles"], [])
    db.conn.execute(
        "INSERT INTO approvals(id,action,resource_ref,request_fingerprint,"
        "request_payload,resource_version,resource_snapshot,state,applicant,"
        "applicant_roles,applicant_role_version,required_role,created_at,expires_at)"
        " VALUES(?,?,?,?,?,?,?, 'pending',?,?,?,?,?,?)",
        (rid, action, version.get("ref", ""), fp,
         json.dumps(body, ensure_ascii=False),
         json.dumps(version, ensure_ascii=False),
         json.dumps(version, ensure_ascii=False),
         actor, json.dumps(roles, ensure_ascii=False), acc["role_version"],
         required_role, t, t + ttl_seconds()))
    db.audit("approval_requested", action, version.get("ref", ""), actor=actor,
             detail={"request_id": rid, "fingerprint": fp,
                     "resource_version": version, "expires_at": t + ttl_seconds()})
    db.commit()
    row = db.conn.execute("SELECT * FROM approvals WHERE id=?", (rid,)).fetchone()
    return {"approval_required": True, "replay": False,
            "request": approval_view(db, row, decisions=False),
            "message": "敏感指令已形成待批事项，等待不同账号复核"}


def _precheck(db, action, body):
    if action == "package_import":
        pkg = core._validate_package(body.get("package", body))
        return _resource_state(db, action, body, allow_missing=True)
    if action == "retry":
        return _resource_state(db, action, body)
    if action == "terminal_ack":
        v = _resource_state(db, action, body)
        r = v.get("receipt")
        if not r or r.get("state") != "rejected_terminal":
            raise core.RelayError(409, "not_terminal",
                                  "目标回执当前不是终态拒绝，不能申请确认重投")
        return v
    if action == "batch_seal":
        v = _resource_state(db, action, body, allow_missing=False)
        if v["ineligible_codes"]:
            # 申请阶段仍执行原有封存资格校验，避免把明显非法操作放入待办；
            # 批准执行瞬间 archive.seal_batch 会再次完整校验。
            reasons = archive.eligibility_reasons(
                db, db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                                   (v["ref"],)).fetchone())
            raise core.RelayError(409, "batch_not_sealable",
                                  "批次当前不满足封存条件", {"reasons": reasons})
        return v
    raise core.RelayError(400, "unknown_action", f"未知敏感动作 {action}")


# ============================================================ 批准/拒绝

def _record_decision(db, request_id, approver, decision, effective, result, detail=None):
    db.conn.execute(
        "INSERT INTO approval_decisions(request_id,ts,approver,decision,effective,"
        "result,detail) VALUES(?,?,?,?,?,?,?)",
        (request_id, now(), approver, decision, 1 if effective else 0, result,
         json.dumps(detail or {}, ensure_ascii=False)))


def approve_request(db, request_id, approver_name, note="", decision="approve"):
    approver = require_login(db, approver_name)
    row = db.conn.execute("SELECT * FROM approvals WHERE id=?",
                          (request_id,)).fetchone()
    if not row:
        raise core.RelayError(404, "not_found", f"待批事项 {request_id} 不存在")
    row = refresh_approval(db, row)
    approver_roles = _loads(approver["roles"], [])

    if row["applicant"] == approver_name:
        return _invalid_decision(db, row, approver_name, decision, note,
                                 "self_approval", "申请人不能批准自己的申请")
    if row["state"] not in PENDING_STATES:
        result = "replay_" + row["state"]
        _record_decision(db, request_id, approver_name, decision, False, result,
                         {"note": note, "state": row["state"]})
        db.audit("approval_decision_replay", row["action"], row["resource_ref"],
                 actor=approver_name, detail={"request_id": request_id, "state": row["state"]})
        db.commit()
        raise core.RelayError(409, result, f"待批事项已处于 {row['state']} 状态",
                              {"request": approval_view(db, request_id),
                               "result": result})

    if row["required_role"] not in approver_roles:
        return _invalid_decision(db, row, approver_name, decision, note,
                                 "unauthorized_role",
                                 f"批准账号缺少岗位 {ROLE_LABELS.get(row['required_role'])}")
    if decision == "reject":
        t = now()
        db.conn.execute(
            "UPDATE approvals SET state='rejected',approver=?,decision_note=?,"
            "decided_at=? WHERE id=? AND state='pending'",
            (approver_name, note, t, request_id))
        _record_decision(db, request_id, approver_name, "reject", True, "rejected",
                         {"note": note})
        db.audit("approval_rejected", row["action"], row["resource_ref"],
                 actor=approver_name,
                 detail={"request_id": request_id, "note": note,
                         "applicant": row["applicant"]})
        db.commit()
        return {"effective": True, "replay": False, "result": "rejected",
                "request": approval_view(db, request_id)}

    # CAS：并发批准只可能有一个 UPDATE 成功；随后的请求看到 approved/executing。
    t = now()
    cur = db.conn.execute(
        "UPDATE approvals SET state='executing',approver=?,decision_note=?,"
        "decision_started_at=? WHERE id=? AND state='pending'",
        (approver_name, note, t, request_id))
    if cur.rowcount != 1:
        _record_decision(db, request_id, approver_name, "approve", False,
                         "concurrent_decision", {"note": note})
        db.commit()
        return {"effective": False, "replay": True, "result": "concurrent_decision",
                "request": approval_view(db, request_id)}
    _record_decision(db, request_id, approver_name, "approve", True, "effective",
                     {"note": note})

    # 执行瞬间再次核对账号/资源版本与原业务校验。
    try:
        fresh_acc = get_account(db, approver_name, active_only=True)
        if row["required_role"] not in _loads(fresh_acc["roles"], []):
            raise _state_invalid("approver_role_lost")
        payload = _loads(row["request_payload"])
        current = _resource_state(db, row["action"], payload, allow_missing=True)
        if _version_changed(_loads(row["resource_version"]), current):
            raise _state_invalid("resource_changed")
        result = _execute(db, row["action"], payload, approver_name)
        db.conn.execute(
            "UPDATE approvals SET state='approved',result_payload=?,"
            "decided_at=?,executed_at=? WHERE id=?",
            (json.dumps(result, ensure_ascii=False), t, now(), request_id))
        db.audit("approval_approved", row["action"], row["resource_ref"],
                 actor=approver_name,
                 detail={"request_id": request_id, "applicant": row["applicant"],
                         "note": note, "result_summary": _summarize_result(result)})
        db.commit()
        return {"effective": True, "replay": False, "result": "approved",
                "request": approval_view(db, request_id), "execution": result}
    except core.RelayError as e:
        # 业务原校验拒绝不是复核系统回滚；该申请终态失败，原目标未被修改。
        # 先提交 CAS 与生效决策，再另起事务记录失败，确保不会回到 pending 被二次批准。
        db.commit()
        final_state = "invalid" if isinstance(e, _StaleApproval) else "failed"
        tt = now()
        db.conn.execute(
            "UPDATE approvals SET state=?,error_code=?,error_message=?,"
            "invalid_reason=CASE WHEN ?='invalid' THEN ? ELSE invalid_reason END,"
            "decided_at=?,executed_at=? WHERE id=?",
            (final_state, e.code, e.message, final_state, e.code, tt, tt, request_id))
        decision_result = "stale_" + e.code if final_state == "invalid" else "execution_rejected"
        db.conn.execute(
            "UPDATE approval_decisions SET effective=0,result=? WHERE id=("
            "SELECT MAX(id) FROM approval_decisions WHERE request_id=?)",
            (decision_result, request_id))
        _record_decision(db, request_id, approver_name, "approve", False,
                         decision_result, {"code": e.code, "message": e.message})
        db.audit("approval_execution_rejected", row["action"], row["resource_ref"],
                 actor=approver_name,
                 detail={"request_id": request_id, "code": e.code, "message": e.message})
        db.commit()
        raise
    except Exception:
        db.conn.rollback()
        raise


class _StaleApproval(core.RelayError):
    pass


class _DirectReplay(core.RelayError):
    def __init__(self, result):
        super().__init__(200, "idempotent_replay", "幂等重放", {"result": result})
        self.result = result


def _state_invalid(reason):
    return _StaleApproval(409, reason, f"待批事项已失效：{reason}", {"reason": reason})


def _invalid_decision(db, row, approver_name, decision, note, result, message):
    _record_decision(db, row["id"], approver_name, decision, False, result,
                     {"note": note, "message": message})
    db.audit("approval_decision_denied", row["action"], row["resource_ref"],
             actor=approver_name,
             detail={"request_id": row["id"], "result": result, "message": message})
    db.commit()
    raise core.RelayError(403 if result == "self_approval" or "unauthorized" in result
                          else 409, result, message,
                          {"request_id": row["id"], "state": row["state"]})


def _execute(db, action, body, actor):
    if action == "package_import":
        return core.import_package(db, body.get("package", body), actor=actor,
                                   source=str(body.get("source", "approved")))
    if action == "retry":
        return core.manual_retry(db, str(body.get("batch_ref", "")),
                                 seq=body.get("seq"), actor=actor,
                                 note=str(body.get("note", "")))
    if action == "terminal_ack":
        return core.acknowledge_and_retry(
            db, str(body.get("batch_ref", "")), int(body["seq"]),
            actor=actor, note=str(body.get("note", "")))
    if action == "batch_seal":
        ref = str(body.get("batch_ref", ""))
        if not ref:
            # 兼容路径 /batches/{ref}/seal 的 body
            ref = str(body.get("resource_ref", ""))
        return archive.seal_batch(db, ref, actor=actor,
                                  note=str(body.get("note", "")))
    raise core.RelayError(400, "unknown_action", f"未知敏感动作 {action}")


def _summarize_result(result):
    if not isinstance(result, dict):
        return {}
    return {k: result.get(k) for k in
            ("batch_ref", "replay", "retried", "sealed", "digest", "state", "status")
            if k in result}


# ============================================================ 视图

def _decisions(db, request_id):
    out = []
    for r in db.conn.execute(
            "SELECT * FROM approval_decisions WHERE request_id=? ORDER BY id",
            (request_id,)):
        d = dict(r)
        d["detail_obj"] = _loads(r["detail"], {})
        out.append(d)
    return out


def approval_view(db, row_or_id, decisions=True):
    row = (db.conn.execute("SELECT * FROM approvals WHERE id=?", (row_or_id,)).fetchone()
           if isinstance(row_or_id, str) else row_or_id)
    if not row:
        return None
    d = dict(row)
    for key in ("request_payload", "resource_version", "resource_snapshot",
                "applicant_roles", "result_payload"):
        d[key + "_obj"] = _loads(row[key], None)
    d["action_label"] = ACTION_LABELS.get(row["action"], row["action"])
    d["required_role_label"] = ROLE_LABELS.get(row["required_role"], row["required_role"])
    d["valid"] = row["state"] in PENDING_STATES
    if decisions:
        d["decisions"] = _decisions(db, row["id"])
    return d


def list_requests(db, scope=None, username=None, include_decisions=False,
                  limit=200, approver_roles=None):
    where, params = [], []
    approver_roles = set(approver_roles or [])
    if scope == "mine":
        where.append("applicant=?")
        params.append(username)
    elif scope == "todo":
        where.append("state='pending'")
        if username:
            where.append("applicant<>?")
            params.append(username)
        if approver_roles:
            marks = ",".join("?" for _ in approver_roles)
            where.append(f"required_role IN ({marks})")
            params.extend(sorted(approver_roles))
    elif scope == "invalid":
        where.append("state IN ('invalid','expired','rejected','failed')")
    sql = "SELECT * FROM approvals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = [refresh_approval(db, r, commit=False) for r in
            db.conn.execute(sql, params).fetchall()]
    db.commit()
    out = []
    for r in rows:
        v = approval_view(db, r, decisions=include_decisions)
        if include_decisions:
            v["decision_count"] = len(v["decisions"])
        out.append(v)
    return out


def sweep_pending(db):
    """后台周期性落账失效检查；任何读/批准路径也会即时刷新，双重保证不使用旧状态。"""
    rows = db.conn.execute(
        "SELECT id FROM approvals WHERE state='pending' ORDER BY created_at").fetchall()
    changed = 0
    for r in rows:
        row = db.conn.execute("SELECT * FROM approvals WHERE id=?",
                              (r["id"],)).fetchone()
        before = row["state"]
        refreshed = refresh_approval(db, row, commit=False)
        if refreshed["state"] != before:
            changed += 1
    if changed:
        db.commit()
    return {"checked": len(rows), "invalidated": changed}


def get_request(db, request_id, username=None):
    row = db.conn.execute("SELECT * FROM approvals WHERE id=?",
                          (request_id,)).fetchone()
    if not row:
        raise core.RelayError(404, "not_found", f"待批事项 {request_id} 不存在")
    row = refresh_approval(db, row)
    return approval_view(db, row, decisions=True)


def decision_log(db, limit=500):
    out = []
    for r in db.conn.execute(
            "SELECT d.*, a.action,a.resource_ref,a.applicant request_applicant,"
            "a.request_fingerprint FROM approval_decisions d JOIN approvals a "
            "ON a.id=d.request_id ORDER BY d.id DESC LIMIT ?", (limit,)):
        d = dict(r)
        d["detail_obj"] = _loads(r["detail"], {})
        out.append(d)
    return out


def _effective_after_crash(db, row):
    """崩溃在 executing 时判断动作是否已经提交，避免恢复时重复执行业务。"""
    payload = _loads(row["request_payload"])
    ref = str(payload.get("batch_ref", ""))
    if row["action"] == "package_import":
        pkg = payload.get("package", payload)
        digest = core.sha256_hex(canonical(pkg))
        hit = db.conn.execute(
            "SELECT batch_ref,imported_at FROM packages WHERE content_hash=?",
            (digest,)).fetchone()
        return ({"replay": True, "batch_ref": hit["batch_ref"],
                 "content_hash": digest, "imported_at": hit["imported_at"],
                 "message": "重启恢复时确认执行包导入已生效"} if hit else None)
    if row["action"] == "batch_seal":
        arch = db.conn.execute("SELECT * FROM archives WHERE batch_ref=?",
                               (ref,)).fetchone()
        return archive._seal_result(arch, replay=True) if arch else None
    if row["action"] in ("retry", "terminal_ack"):
        # 重试/重投只是状态转换；若目标已不在原可重试状态，则视为动作已生效。
        seq = payload.get("seq")
        if row["action"] == "retry" and seq is None:
            rows = db.conn.execute(
                "SELECT state FROM receipts WHERE batch_ref=? AND state IN "
                "('retry_wait','dispatching')", (ref,)).fetchall()
            if rows:
                return None
            return {"retried": [], "message": "重启恢复时确认批量重试已生效"}
        r = db.conn.execute(
            "SELECT state FROM receipts WHERE batch_ref=? AND seq=?",
            (ref, int(seq))).fetchone()
        if not r:
            return None
        if r["state"] in ("retry_wait", "dispatching"):
            return None
        return {"retried": [int(seq)],
                "message": f"重启恢复时确认目标已离开可重试状态（{r['state']}）"}
    return None


def recover_executing(db):
    """服务重启后恢复 executing 待办。业务动作保持幂等/前置校验，不能绕过业务规则。"""
    rows = db.conn.execute("SELECT * FROM approvals WHERE state='executing'").fetchall()
    recovered = []
    for row in rows:
        effective = _effective_after_crash(db, row)
        if effective:
            db.conn.execute(
                "UPDATE approvals SET state='approved',result_payload=?,"
                "executed_at=? WHERE id=?",
                (json.dumps(effective, ensure_ascii=False), now(), row["id"]))
            db.audit("approval_recovered", row["action"], row["resource_ref"],
                     actor=row["approver"] or "recovery",
                     detail={"request_id": row["id"], "mode": "observed_effect"})
            recovered.append({"id": row["id"], "state": "approved"})
            continue
        try:
            payload = _loads(row["request_payload"])
            current = _resource_state(db, row["action"], payload, allow_missing=True)
            if _version_changed(_loads(row["resource_version"]), current):
                raise _state_invalid("resource_changed")
            result = _execute(db, row["action"], payload,
                              row["approver"] or "recovery")
            db.conn.execute(
                "UPDATE approvals SET state='approved',result_payload=?,"
                "executed_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), now(), row["id"]))
            db.audit("approval_recovered", row["action"], row["resource_ref"],
                     actor=row["approver"] or "recovery",
                     detail={"request_id": row["id"]})
            recovered.append({"id": row["id"], "state": "approved"})
        except core.RelayError as e:
            db.conn.rollback()
            db.conn.execute(
                "UPDATE approvals SET state='failed',error_code=?,error_message=?,"
                "executed_at=? WHERE id=?", (e.code, e.message, now(), row["id"]))
            db.audit("approval_recovery_failed", row["action"], row["resource_ref"],
                     actor=row["approver"] or "recovery",
                     detail={"request_id": row["id"], "code": e.code, "message": e.message})
            recovered.append({"id": row["id"], "state": "failed", "code": e.code})
        except Exception as e:  # noqa: BLE001
            db.conn.rollback()
            db.conn.execute(
                "UPDATE approvals SET state='failed',error_code='recovery_error',"
                "error_message=?,executed_at=? WHERE id=?", (repr(e), now(), row["id"]))
            recovered.append({"id": row["id"], "state": "failed", "code": "recovery_error"})
    db.commit()
    return {"executing_recovered": len(rows), "items": recovered}
