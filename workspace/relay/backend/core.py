"""中继核心逻辑：执行包不可变存档、回执入账/冲突判定、视图、手动重试与启动恢复。

所有函数假定调用方持有 db.lock（HTTP 线程与转发线程串行）。
顺序与恰好一次语义：
  * 每批次独立游标 cursors.next_seq；
  * seq < next_seq → 该序号已有终局，仅允许**完全相同**回执重放（返回原结果）；
  * seq == next_seq → queued 等待派发；
  * seq > next_seq → buffered 持久缓冲，等缺口补齐后自动转 queued；
  * 同 seq 或同 receipt_no 但内容不同 → 409 拒绝，记 conflicts，不推进游标；
  * rejected_terminal 冻结游标（next_seq 不动），后续回执全部缓冲、无法越过。
"""
import hashlib
import json
import time

VALID_TYPES = ("departed", "failed_to_depart", "arrived")
TYPE_LABELS = {"departed": "已离场", "failed_to_depart": "未能出发", "arrived": "到达确认"}
STATE_LABELS = {
    "buffered": "缓冲(缺序号)", "queued": "待转发", "dispatching": "转发中",
    "retry_wait": "等待重试", "forwarded": "已转发(终态成功)",
    "rejected_terminal": "终态拒绝(阻塞)",
}


class RelayError(Exception):
    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


def canonical(obj):
    """规范化 JSON：排序键、无空白（对字典/列表递归排序后 json.dumps）。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now():
    return time.time()


# ============================================================ 执行包

REQUIRED_PKG_FIELDS = ("batch_ref", "batch_summary")


def _validate_package(raw):
    if not isinstance(raw, dict):
        raise RelayError(400, "malformed", "执行包必须是 JSON 对象")
    for f in REQUIRED_PKG_FIELDS:
        if not str(raw.get(f, "")).strip():
            raise RelayError(400, "malformed", f"执行包缺少必需字段 {f}")
    if not isinstance(raw.get("items", []), list):
        raise RelayError(400, "malformed", "执行包 items 必须是数组")
    return raw


def import_package(db, raw, actor="api", source=""):
    """导入不可变执行包。

    幂等：完全相同的包再次导入 → 返回首次导入结果（replay=true）。
    冲突：同 batch_ref 但内容不同 → 409，不覆盖原包。
    """
    raw = _validate_package(raw)
    batch_ref = str(raw["batch_ref"]).strip()
    content_hash = sha256_hex(canonical(raw))
    t = now()

    existing_hash = db.conn.execute(
        "SELECT content_hash FROM packages WHERE content_hash=?", (content_hash,)).fetchone()
    existing = db.conn.execute(
        "SELECT * FROM packages WHERE batch_ref=?", (batch_ref,)).fetchone()

    if existing_hash:
        # 完全相同的再次导入 → 返回原结果
        db.audit("package_import_replay", batch_ref, actor=actor,
                 detail={"content_hash": content_hash})
        db.commit()
        return {"replay": True, "batch_ref": batch_ref, "content_hash": content_hash,
                "imported_at": existing_hash_ts(db, batch_ref),
                "message": "执行包内容完全一致，返回首次导入结果"}

    if existing:
        detail = {"batch_ref": batch_ref,
                  "existing_hash": existing["content_hash"], "new_hash": content_hash}
        db.add_conflict("package_conflict", {"batch_ref": batch_ref,
                                             "_content_hash": content_hash, "_raw": raw},
                        "同 batch_ref 的执行包内容不同；原包不可变")
        db.audit("package_import_conflict", batch_ref, actor=actor, detail=detail)
        db.commit()
        raise RelayError(409, "package_conflict",
                         f"批次 {batch_ref} 已存在内容不同的执行包（不可变）", detail)

    seq_base = int(raw.get("seq_base", 1) or 1)
    package_id = str(raw.get("package_id", ""))
    db.conn.execute(
        "INSERT INTO packages(batch_ref,package_id,batch_summary,seq_base,content_hash,"
        "payload,source,imported_at) VALUES(?,?,?,?,?,?,?,?)",
        (batch_ref, package_id, str(raw["batch_summary"]), seq_base, content_hash,
         json.dumps(raw, ensure_ascii=False), source or raw.get("source", ""), t))
    db.conn.execute(
        "INSERT INTO cursors(batch_ref,next_seq) VALUES(?,?) "
        "ON CONFLICT(batch_ref) DO NOTHING", (batch_ref, seq_base))
    db.audit("package_imported", batch_ref, actor=actor,
             detail={"content_hash": content_hash, "package_id": package_id,
                     "items": len(raw.get("items", []))})
    db.commit()
    return {"replay": False, "batch_ref": batch_ref, "content_hash": content_hash,
            "imported_at": t, "message": "执行包已不可变存档"}


def existing_hash_ts(db, batch_ref):
    r = db.conn.execute("SELECT imported_at FROM packages WHERE batch_ref=?",
                        (batch_ref,)).fetchone()
    return r["imported_at"] if r else None


# ============================================================ 回执入账

REQUIRED_RCPT_FIELDS = ("seq", "receipt_no", "type", "batch_summary")


def _normalize_receipt_input(item, default_batch=None):
    if not isinstance(item, dict):
        raise RelayError(400, "malformed", "回执必须是 JSON 对象")
    if "batch_ref" not in item and default_batch:
        item = dict(item)
        item["batch_ref"] = default_batch
    return item


def _validate_receipt(db, raw):
    """返回 (pkg, seq)。非法输入直接抛 RelayError（不在此处提交，由调用方管事务）。"""
    for f in REQUIRED_RCPT_FIELDS:
        if f not in raw or raw.get(f) is None or str(raw.get(f, "")).strip() == "":
            raise RelayError(400, "malformed", f"回执缺少必需字段 {f}")
    batch_ref = str(raw["batch_ref"]).strip()
    pkg = db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    if not pkg:
        db.add_conflict("unknown_batch", {"_raw": raw,
                                          "_content_hash": "", **raw} if isinstance(raw, dict)
                        else {"_raw": raw}, f"批次 {batch_ref} 未导入执行包")
        raise RelayError(404, "unknown_batch", f"批次 {batch_ref} 的执行包尚未导入")
    if str(raw["batch_summary"]) != pkg["batch_summary"]:
        db.add_conflict("summary_mismatch", {"_raw": raw, **raw},
                        "回执批次摘要与执行包不一致")
        db.audit("receipt_summary_mismatch", batch_ref,
                 seq=raw.get("seq"), receipt_no=str(raw.get("receipt_no", "")),
                 detail={"expected": pkg["batch_summary"],
                         "got": str(raw["batch_summary"])})
        raise RelayError(409, "summary_mismatch",
                         "回执批次摘要与执行包不一致（拒绝且不推进游标）",
                         {"expected": pkg["batch_summary"]})
    if str(raw["type"]) not in VALID_TYPES:
        raise RelayError(400, "malformed",
                         f"回执类型非法（支持 {', '.join(VALID_TYPES)}）")
    try:
        seq = int(raw["seq"])
    except (TypeError, ValueError):
        raise RelayError(400, "malformed", "seq 必须是整数")
    if seq < pkg["seq_base"]:
        raise RelayError(400, "malformed",
                         f"seq 不能小于批次序号起点 {pkg['seq_base']}")
    return pkg, seq


def ingest_receipt(db, raw, actor="api", commit=True):
    """接收并持久化一条回执。返回入账结果（不直接执行回调；转发器异步派发）。

    commit=False 时只写事务不提交，供批量上传在外层统一提交/回滚。
    """
    raw = _normalize_receipt_input(raw)
    pkg, seq = _validate_receipt(db, raw)
    batch_ref = pkg["batch_ref"]
    receipt_no = str(raw["receipt_no"]).strip()
    rtype = str(raw["type"])
    cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    next_seq = cur["next_seq"]
    content_hash = sha256_hex(canonical(raw))

    # ---- 已封存批次：完全相同→归档重放；否则拒绝。归档记录永不回到转发队列 ----
    if pkg["archived"]:
        import archive  # 延迟导入避免循环依赖
        return archive.ingest_into_archived(db, pkg, raw, content_hash,
                                            actor=actor, commit=commit)

    row = db.conn.execute(
        "SELECT * FROM receipts WHERE batch_ref=? AND seq=?", (batch_ref, seq)).fetchone()
    same_no = None if row else db.conn.execute(
        "SELECT * FROM receipts WHERE batch_ref=? AND receipt_no=?",
        (batch_ref, receipt_no)).fetchone()

    # ---- 已存在相同序号：完全一致→重放原结果；内容不同→冲突拒绝 ----
    if row:
        if row["content_hash"] == content_hash:
            return {"seq": seq, "receipt_no": receipt_no, "state": row["state"],
                    "status": "duplicate_replay", "result_code": row["result_code"],
                    "message": "完全相同的回执已存在，返回原结果（幂等重放）"}
        detail = {"seq": seq, "existing_no": row["receipt_no"], "new_no": receipt_no,
                  "existing_hash": row["content_hash"], "new_hash": content_hash}
        db.add_conflict("conflict_seq", {"_raw": raw, "_content_hash": content_hash,
                                         "batch_ref": batch_ref, "seq": seq,
                                         "receipt_no": receipt_no},
                        "同序号回执内容与既有回执不同")
        db.audit("receipt_conflict_seq", batch_ref, seq=seq,
                 receipt_no=receipt_no, actor=actor, detail=detail)
        raise RelayError(409, "conflict_seq",
                         f"序号 {seq} 已有内容不同的回执（拒绝且不推进游标）", detail)

    # ---- 回执编号在本批次内唯一（不同 seq 复用编号也算冲突）----
    if same_no:
        detail = {"receipt_no": receipt_no, "existing_seq": same_no["seq"],
                  "new_seq": seq, "existing_hash": same_no["content_hash"],
                  "new_hash": content_hash}
        db.add_conflict("conflict_receipt_no",
                        {"_raw": raw, "_content_hash": content_hash,
                         "batch_ref": batch_ref, "seq": seq, "receipt_no": receipt_no},
                        "回执编号已被不同序号的回执使用")
        db.audit("receipt_conflict_no", batch_ref, seq=seq,
                 receipt_no=receipt_no, actor=actor, detail=detail)
        raise RelayError(409, "conflict_receipt_no",
                         f"回执编号 {receipt_no} 已被序号 {same_no['seq']} 使用（拒绝）",
                         detail)

    if seq < next_seq:
        # 游标已越过该序号但库里没有该序号的行：只能是历史终态被外部清理，按冲突拒绝
        raise RelayError(409, "seq_behind_cursor",
                         f"序号 {seq} 已落后于游标 {next_seq} 且无原始记录（拒绝）",
                         {"cursor": next_seq})

    state = "queued" if seq == next_seq else "buffered"
    t = now()
    db.conn.execute(
        "INSERT INTO receipts(batch_ref,seq,receipt_no,receipt_type,content_hash,payload,"
        "state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (batch_ref, seq, receipt_no, rtype, content_hash,
         json.dumps(raw, ensure_ascii=False), state, t, t))
    db.audit("receipt_received", batch_ref, seq=seq, receipt_no=receipt_no, actor=actor,
             detail={"type": rtype, "state": state, "cursor": next_seq})
    if commit:
        db.commit()
    return {"seq": seq, "receipt_no": receipt_no, "state": state,
            "status": "accepted", "message": "回执已持久化"
            + ("，等待转发" if state == "queued" else f"，缺口前需补齐序号 {next_seq}..{seq - 1}")}


def ingest_batch(db, body, actor="api"):
    """批量上传：{"batch_ref":..,"batch_summary":..,"receipts":[...]}。

    整体在一个事务内逐条校验；任一条被拒（冲突/摘要不符/未知批次）则全部不生效，
    返回 409 及逐条结果；完全相同的整批再次上传返回全部 duplicate_replay。
    """
    if not isinstance(body, dict):
        raise RelayError(400, "malformed", "请求体必须是 JSON 对象")
    batch_ref = str(body.get("batch_ref", "")).strip()
    if not batch_ref:
        raise RelayError(400, "malformed", "缺少 batch_ref")
    items = body.get("receipts")
    if not isinstance(items, list) or not items:
        raise RelayError(400, "malformed", "receipts 必须是非空数组")

    # 批内自身的 seq / receipt_no 重复先拦截（内容不同才算冲突；完全相同允许折叠）
    seen_seq, seen_no = {}, {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise RelayError(400, "malformed", f"第 {i} 条回执不是对象")
        it2 = _normalize_receipt_input(it, default_batch=batch_ref)
        if not str(it2.get("batch_summary", "")).strip() and body.get("batch_summary"):
            it2 = dict(it2)
            it2["batch_summary"] = body["batch_summary"]
        items[i] = it2
        s, n = it2.get("seq"), str(it2.get("receipt_no", ""))
        if s in seen_seq and canonical(it2) != canonical(items[seen_seq[s]]):
            raise RelayError(409, "batch_dup_seq", f"批内序号 {s} 出现内容不同的两条")
        seen_seq.setdefault(s, i)
        if n in seen_no and canonical(it2) != canonical(items[seen_no[n]]):
            raise RelayError(409, "batch_dup_receipt_no",
                             f"批内回执编号 {n} 出现内容不同的两条")
        seen_no.setdefault(n, i)

    results = []
    failed_index = None
    try:
        for it in items:
            # 整批共用一个事务：ingest_receipt 不自行提交
            results.append(ingest_receipt(db, it, actor=actor, commit=False))
        db.commit()  # 全部入账成功才统一提交
    except RelayError as e:
        # 单条失败 → 回滚所有已插入回执/审计/冲突；另起事务仅留拒绝审计
        db.conn.rollback()
        failed_index = len(results)
        db.audit("receipt_batch_rejected", batch_ref, actor=actor,
                 detail={"code": e.code, "index": failed_index, "message": e.message})
        db.commit()
        raise RelayError(e.status, e.code,
                         f"批量上传被整体拒绝：{e.message}",
                         {"failed_item": e.extra, "index": failed_index})
    return {"batch_ref": batch_ref, "count": len(results), "results": results}


# ============================================================ 视图

def list_batches(db):
    out = []
    for p in db.conn.execute("SELECT * FROM packages ORDER BY imported_at DESC"):
        out.append(_batch_summary(db, p))
    return out


def get_batch_detail(db, batch_ref):
    p = db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                        (batch_ref,)).fetchone()
    if not p:
        raise RelayError(404, "not_found", f"批次 {batch_ref} 不存在")
    view = _batch_summary(db, p)
    if p["archived"]:
        # 已封存：从只读归档恢复详情（校验摘要 + 回执明细 + 原审计时间线）
        import archive
        detail = archive.get_archive_detail(db, batch_ref, verify=True)
        view["receipts"] = detail["receipts"]
        view["audit"] = detail["audit"]
        view["verification"] = detail["verification"]
        view["readonly"] = True
        view["payload"] = json.loads(p["payload"])
        return view
    receipts = []
    for r in db.conn.execute(
            "SELECT * FROM receipts WHERE batch_ref=? ORDER BY seq", (batch_ref,)):
        receipts.append(_receipt_view(r))
    view["receipts"] = receipts
    view["payload"] = json.loads(p["payload"])
    return view


def _batch_summary(db, prow):
    ref = str(prow["batch_ref"])
    archived = bool(prow["archived"])
    cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                          (ref,)).fetchone()
    next_seq = cur["next_seq"] if cur else prow["seq_base"]
    rows = db.conn.execute(
        "SELECT state,COUNT(*) c,MAX(seq) max_seq,MIN(seq) min_seq FROM receipts"
        " WHERE batch_ref=? GROUP BY state", (ref,)).fetchall()
    by_state = {r["state"]: r["c"] for r in rows}
    max_seen = max((r["max_seq"] for r in rows if r["max_seq"] is not None), default=None)

    buffered = [dict(r) for r in db.conn.execute(
        "SELECT seq,receipt_no,receipt_type,created_at FROM receipts"
        " WHERE batch_ref=? AND state='buffered' ORDER BY seq", (ref,))]
    waiters = [dict(r) for r in db.conn.execute(
        "SELECT seq,receipt_no,receipt_type,next_retry_at FROM receipts"
        " WHERE batch_ref=? AND state IN ('retry_wait','dispatching') ORDER BY seq",
        (ref,))]

    # 缺口 = [next_seq .. max_seen] 中没有回执行的序号
    gaps, present = [], set()
    if max_seen is not None:
        present = {x[0] for x in db.conn.execute(
            "SELECT seq FROM receipts WHERE batch_ref=?", (ref,))}
        gaps = [s for s in range(next_seq, max_seen + 1) if s not in present]

    blocker = db.conn.execute(
        "SELECT seq,receipt_no,result_code,result_detail,rejected_at FROM receipts"
        " WHERE batch_ref=? AND state='rejected_terminal' ORDER BY seq LIMIT 1",
        (ref,)).fetchone()
    archive_meta = None
    if archived:
        a = db.conn.execute(
            "SELECT digest,sealed_at,sealed_by,receipt_count,audit_count,freed_bytes"
            " FROM archives WHERE batch_ref=?", (ref,)).fetchone()
        archive_meta = dict(a) if a else None
    status = "idle"
    if archived:
        status = "archived"
    elif blocker:
        status = "blocked_terminal"
    elif by_state.get("dispatching") or by_state.get("retry_wait"):
        status = "forwarding"
    elif by_state.get("buffered") or by_state.get("queued"):
        status = "collecting"
    elif by_state.get("forwarded"):
        status = "all_forwarded"

    return {
        "batch_ref": ref, "package_id": prow["package_id"],
        "batch_summary": prow["batch_summary"], "seq_base": prow["seq_base"],
        "content_hash": prow["content_hash"], "source": prow["source"],
        "imported_at": prow["imported_at"],
        "archived": archived, "archive": archive_meta,
        "cursor": next_seq, "max_seen": max_seen, "gaps": gaps,
        "buffered_items": buffered, "in_flight": waiters,
        "counts": {"buffered": by_state.get("buffered", 0),
                   "queued": by_state.get("queued", 0),
                   "dispatching": by_state.get("dispatching", 0),
                   "retry_wait": by_state.get("retry_wait", 0),
                   "forwarded": by_state.get("forwarded", 0),
                   "rejected_terminal": by_state.get("rejected_terminal", 0)},
        "blocker": dict(blocker) if blocker else None,
        "status": status,
    }


def _receipt_view(r):
    return {
        "seq": r["seq"], "receipt_no": r["receipt_no"], "type": r["receipt_type"],
        "type_label": TYPE_LABELS.get(r["receipt_type"], r["receipt_type"]),
        "state": r["state"], "state_label": STATE_LABELS.get(r["state"], r["state"]),
        "attempts": r["attempts"], "last_error": r["last_error"],
        "result_code": r["result_code"], "result_detail": r["result_detail"],
        "next_retry_at": r["next_retry_at"], "lease_until": r["lease_until"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "forwarded_at": r["forwarded_at"], "rejected_at": r["rejected_at"],
    }


def queue_overview(db):
    pending = [dict(r) for r in db.conn.execute(
        "SELECT batch_ref,seq,receipt_no,receipt_type,state,attempts,last_error,"
        "result_code,next_retry_at FROM receipts"
        " WHERE state IN ('queued','buffered','retry_wait','dispatching','rejected_terminal')"
        " ORDER BY batch_ref,seq")]
    return {"pending": pending, "total": len(pending)}


def recent_audit(db, limit=200):
    out = []
    for r in db.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(r)
        try:
            d["detail_obj"] = json.loads(d["detail"])
        except ValueError:
            d["detail_obj"] = None
        out.append(d)
    return out


def recent_conflicts(db, limit=100):
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM conflicts ORDER BY id DESC LIMIT ?", (limit,))]


# ============================================================ 手动重试 / 恢复

def manual_retry(db, batch_ref, seq=None, actor="operator", note=""):
    """失败重试入口。

    * retry_wait（瞬时失败退避中）→ 立即回到 queued；
    * rejected_terminal（终态拒绝）→ 默认拒绝；只有明确确认主系统已解除终态
      （批次恢复/车辆或目的地改回）时，携带 acknowledge_terminal=true 才允许重投，
      全程审计。游标从未被该终态项推进，因此重投成功后仍按序前推。
    """
    rows_q = "batch_ref=?"
    params = [batch_ref]
    if seq is not None:
        rows_q += " AND seq=?"
        params.append(seq)
    pkg = db.conn.execute("SELECT archived FROM packages WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    if pkg and pkg["archived"]:
        raise RelayError(409, "batch_archived",
                         f"批次 {batch_ref} 已封存为只读归档，"
                         "归档记录不再进入转发队列")
    rows = db.conn.execute(
        f"SELECT * FROM receipts WHERE {rows_q} ORDER BY seq", params).fetchall()
    if not rows:
        raise RelayError(404, "not_found", "没有匹配的可重试回执")
    changed = []
    for r in rows:
        if r["state"] in ("retry_wait", "dispatching"):
            db.conn.execute(
                "UPDATE receipts SET state='queued',last_error=last_error,"
                "next_retry_at=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (now(), r["id"]))
            db.audit("manual_retry", batch_ref, seq=r["seq"],
                     receipt_no=r["receipt_no"], actor=actor,
                     detail={"from": r["state"], "note": note})
            changed.append(r["seq"])
        elif r["state"] == "rejected_terminal":
            raise RelayError(409, "terminal_requires_ack",
                             f"序号 {r['seq']} 为终态拒绝（{r['result_code']}），"
                             "普通重试不得越过；如主系统已解除，请确认后重投",
                             {"seq": r["seq"], "reason": r["result_code"]})
    if not changed:
        raise RelayError(409, "nothing_to_retry",
                         "匹配回执均不处于可重试状态（forwarded/buffered/queued）")
    db.commit()
    return {"retried": changed, "message": f"已重新入队 {len(changed)} 条"}


def acknowledge_and_retry(db, batch_ref, seq, actor="operator", note=""):
    pkg = db.conn.execute("SELECT archived FROM packages WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    if pkg and pkg["archived"]:
        raise RelayError(409, "batch_archived",
                         f"批次 {batch_ref} 已封存为只读归档，"
                         "归档记录不再进入转发队列")
    r = db.conn.execute(
        "SELECT * FROM receipts WHERE batch_ref=? AND seq=?",
        (batch_ref, seq)).fetchone()
    if not r:
        raise RelayError(404, "not_found", "回执不存在")
    if r["state"] != "rejected_terminal":
        raise RelayError(409, "not_terminal",
                         f"序号 {seq} 当前为 {r['state']}，无需终态确认")
    db.conn.execute(
        "UPDATE receipts SET state='queued',next_retry_at=NULL,lease_until=NULL,"
        "updated_at=? WHERE id=?", (now(), r["id"]))
    db.audit("terminal_ack_retry", batch_ref, seq=seq,
             receipt_no=r["receipt_no"], actor=actor,
             detail={"previous_reason": r["result_code"], "note": note})
    db.commit()
    return {"retried": [seq], "message": f"序号 {seq} 已在人工确认终态解除后重新入队"}


def recover_on_startup(db):
    """进程重启恢复：把租约过期的 dispatching 行安全回收为 queued。

    forwarded 行永不被回收 → 不会重复调用已成功的回调；
    retry_wait 保持其退避时刻不变。
    """
    t = now()
    stale = db.conn.execute(
        "SELECT batch_ref,seq,receipt_no,attempts FROM receipts"
        " WHERE state='dispatching' AND (lease_until IS NULL OR lease_until<?)",
        (t,)).fetchall()
    for r in stale:
        db.conn.execute(
            "UPDATE receipts SET state='queued',lease_until=NULL,next_retry_at=NULL,"
            "updated_at=? WHERE batch_ref=? AND seq=?",
            (t, r["batch_ref"], r["seq"]))
        db.audit("startup_recovery", r["batch_ref"], seq=r["seq"],
                 receipt_no=r["receipt_no"],
                 detail={"previous_attempts": r["attempts"]})
    pending = db.conn.execute(
        "SELECT COUNT(*) c FROM receipts WHERE state IN "
        "('queued','retry_wait','dispatching','buffered')").fetchone()["c"]
    # 归档一致性防御校正：封存是单事务（归档行/热库删除/封存标记同生共死），
    # 正常不会触发；若外部破坏导致标记与归档不一致，按归档为准回正。
    repaired = 0
    for r in db.conn.execute("SELECT batch_ref,archived FROM packages").fetchall():
        has = db.conn.execute("SELECT 1 FROM archives WHERE batch_ref=?",
                              (r["batch_ref"],)).fetchone()
        if r["archived"] and not has:
            db.conn.execute(
                "UPDATE packages SET archived=0,archived_at=NULL WHERE batch_ref=?",
                (r["batch_ref"],))
            db.audit("archive_consistency_repair", r["batch_ref"],
                     detail={"issue": "archived_flag_without_archive"})
            repaired += 1
        elif has and not r["archived"]:
            db.conn.execute(
                "UPDATE packages SET archived=1 WHERE batch_ref=?",
                (r["batch_ref"],))
            db.audit("archive_consistency_repair", r["batch_ref"],
                     detail={"issue": "archive_without_flag"})
            repaired += 1
    db.audit("relay_started", detail={"recovered_leases": len(stale),
                                      "pending_rows": pending,
                                      "archive_repairs": repaired})
    db.commit()
    return {"recovered_leases": len(stale), "pending_rows": pending,
            "archive_repairs": repaired}
