"""批次封存与只读归档 —— 中继磁盘按批次回收空间。

封存资格（必须全部满足，否则 409 batch_not_sealable 并逐条列出原因）：
  * 无序号缺口；
  * 无待转发(queued)/转发中(dispatching)/待重试(retry_wait)/缓冲(buffered)项；
  * 无终态阻塞(rejected_terminal)项 —— 即全部回执已结束(forwarded)；
  * 封存前完整性校验通过（执行包与每条回执的内容哈希与原文一致，
    否则 409 checksum_mismatch）。

封存是**单个 SQLite 事务**：构建内容清单(manifest)与校验摘要(digest) →
写入只读归档(archives) → 删除热库回执明细与该批次审计行（原审计时间线完整
保存在归档 payload 内）→ 标记执行包已封存。事务中途失败或进程崩溃整体回滚，
不会留下半迁移状态；重启后要么完全未封存、要么已完整封存。

封存之后：
  * 同批次重复封存直接返回第一次结果（幂等 replay）；
  * 该批次新回执一律 409 batch_archived 拒绝；完全相同的历史回执按归档重放
    （duplicate_replay）——归档记录永不再次进入转发队列；
  * 归档可整体下载、可重算摘要校验，校验通过后恢复只读详情（含原审计时间线）。

所有函数假定调用方持有 db.lock（与 HTTP 线程/转发线程串行）。
"""
import json

import core
from core import RelayError, canonical, now, sha256_hex

ARCHIVE_FORMAT = "relay-archive/v1"

NON_FORWARDED_LABELS = {
    "queued": "待转发", "dispatching": "转发中", "retry_wait": "待重试",
    "buffered": "缺口缓冲", "rejected_terminal": "终态阻塞",
}


# ============================================================ 资格与预估

def _get_pkg(db, batch_ref):
    pkg = db.conn.execute("SELECT * FROM packages WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    if not pkg:
        raise RelayError(404, "not_found", f"批次 {batch_ref} 不存在")
    return pkg


def _get_archive(db, batch_ref):
    arch = db.conn.execute("SELECT * FROM archives WHERE batch_ref=?",
                           (batch_ref,)).fetchone()
    if arch:
        return arch
    pkg = db.conn.execute("SELECT archived FROM packages WHERE batch_ref=?",
                          (batch_ref,)).fetchone()
    if pkg is None:
        raise RelayError(404, "not_found", f"批次 {batch_ref} 不存在")
    raise RelayError(409, "not_archived", f"批次 {batch_ref} 尚未封存，没有归档")


def _gaps(db, pkg):
    ref = pkg["batch_ref"]
    cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                          (ref,)).fetchone()
    next_seq = cur["next_seq"] if cur else pkg["seq_base"]
    rows = db.conn.execute("SELECT seq FROM receipts WHERE batch_ref=?",
                           (ref,)).fetchall()
    if not rows:
        return [], next_seq
    present = {r["seq"] for r in rows}
    return [s for s in range(next_seq, max(present) + 1) if s not in present], next_seq


def eligibility_reasons(db, pkg):
    """返回不可封存原因列表（空列表 = 可封存）。"""
    if pkg["archived"]:
        return [{"code": "already_archived", "message": "批次已封存"}]
    reasons = []
    gaps, next_seq = _gaps(db, pkg)
    if gaps:
        reasons.append({"code": "has_gaps",
                        "message": f"存在序号缺口 [{', '.join(map(str, gaps))}]"
                                   f"（游标待 {next_seq}）",
                        "gaps": gaps})
    rows = db.conn.execute(
        "SELECT state,COUNT(*) c FROM receipts WHERE batch_ref=? GROUP BY state",
        (pkg["batch_ref"],)).fetchall()
    for r in rows:
        if r["state"] != "forwarded" and r["c"]:
            label = NON_FORWARDED_LABELS.get(r["state"], r["state"])
            reasons.append({"code": f"pending_{r['state']}",
                            "message": f"存在 {r['c']} 条「{label}」回执，尚未结束",
                            "state": r["state"], "count": r["c"]})
    return reasons


def _hot_usage(db, batch_ref):
    r = db.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) b"
        " FROM receipts WHERE batch_ref=?", (batch_ref,)).fetchone()
    a = db.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(LENGTH(CAST(detail AS BLOB))),0) b"
        " FROM audit_log WHERE batch_ref=?", (batch_ref,)).fetchone()
    return {"receipt_count": r["c"], "receipt_bytes": r["b"],
            "audit_count": a["c"], "audit_bytes": a["b"],
            "hot_bytes": r["b"] + a["b"]}


def estimate_storage(db):
    """全部批次的热库占用预估与封存资格（管理页「预估占用」）。"""
    batches = []
    totals = {"hot_bytes": 0, "archive_bytes": 0, "freed_bytes": 0,
              "sealable": 0, "archived": 0}
    for p in db.conn.execute("SELECT * FROM packages ORDER BY imported_at"):
        ref = p["batch_ref"]
        if p["archived"]:
            arch = db.conn.execute(
                "SELECT digest,sealed_at,sealed_by,receipt_count,audit_count,"
                "freed_bytes,LENGTH(CAST(payload AS BLOB)) size,"
                "LENGTH(CAST(manifest AS BLOB)) msize FROM archives WHERE batch_ref=?",
                (ref,)).fetchone()
            entry = {"batch_ref": ref, "archived": True, "sealable": False,
                     "reasons": [], "hot_bytes": 0, "receipt_count": 0,
                     "receipt_bytes": 0, "audit_count": 0, "audit_bytes": 0,
                     "archive": dict(arch) if arch else None}
            if arch:
                totals["archive_bytes"] += arch["size"] + arch["msize"]
                totals["freed_bytes"] += arch["freed_bytes"]
                totals["archived"] += 1
        else:
            usage = _hot_usage(db, ref)
            reasons = eligibility_reasons(db, p)
            entry = {"batch_ref": ref, "archived": False,
                     "sealable": not reasons, "reasons": reasons, **usage}
            totals["hot_bytes"] += usage["hot_bytes"]
            if not reasons:
                totals["sealable"] += 1
        batches.append(entry)
    return {"batches": batches, "totals": totals}


# ============================================================ 完整性校验

def _integrity_errors(db, pkg, receipts):
    """封存前校验：执行包与每条回执的规范化内容哈希必须与入账时一致。"""
    errs = []
    try:
        pkg_hash = sha256_hex(canonical(json.loads(pkg["payload"])))
    except ValueError:
        pkg_hash = "<unparseable>"
    if pkg_hash != pkg["content_hash"]:
        errs.append({"kind": "package", "expected": pkg["content_hash"],
                     "actual": pkg_hash})
    for r in receipts:
        try:
            h = sha256_hex(canonical(json.loads(r["payload"])))
        except ValueError:
            h = "<unparseable>"
        if h != r["content_hash"]:
            errs.append({"kind": "receipt", "seq": r["seq"],
                         "receipt_no": r["receipt_no"],
                         "expected": r["content_hash"], "actual": h})
    return errs


# ============================================================ 封存

def _build_archive_payload(db, pkg, receipts, audit_rows, sealed_at, sealed_by, note):
    cur = db.conn.execute("SELECT next_seq FROM cursors WHERE batch_ref=?",
                          (pkg["batch_ref"],)).fetchone()
    manifest = {
        "format": ARCHIVE_FORMAT,
        "batch_ref": pkg["batch_ref"],
        "package_id": pkg["package_id"],
        "batch_summary": pkg["batch_summary"],
        "seq_base": pkg["seq_base"],
        "package_hash": pkg["content_hash"],
        "package_imported_at": pkg["imported_at"],
        "cursor_at_seal": cur["next_seq"] if cur else pkg["seq_base"],
        "receipt_count": len(receipts),
        "receipts": [
            {"seq": r["seq"], "receipt_no": r["receipt_no"],
             "type": r["receipt_type"], "content_hash": r["content_hash"],
             "state": r["state"], "result_code": r["result_code"],
             "attempts": r["attempts"], "forwarded_at": r["forwarded_at"]}
            for r in receipts],
        "audit_count": len(audit_rows),
        "audit_first_ts": audit_rows[0]["ts"] if audit_rows else None,
        "audit_last_ts": audit_rows[-1]["ts"] if audit_rows else None,
        "sealed_at": sealed_at, "sealed_by": sealed_by, "note": note,
    }
    payload = {
        "format": ARCHIVE_FORMAT,
        "manifest": manifest,
        "package": json.loads(pkg["payload"]),
        "receipts": [
            {"seq": r["seq"], "receipt_no": r["receipt_no"],
             "type": r["receipt_type"], "content_hash": r["content_hash"],
             "state": r["state"], "attempts": r["attempts"],
             "result_code": r["result_code"], "result_detail": r["result_detail"],
             "created_at": r["created_at"], "updated_at": r["updated_at"],
             "forwarded_at": r["forwarded_at"],
             "payload": json.loads(r["payload"])}
            for r in receipts],
        "audit": [
            {"ts": a["ts"], "kind": a["kind"], "seq": a["seq"],
             "receipt_no": a["receipt_no"], "actor": a["actor"],
             "detail": a["detail"]}
            for a in audit_rows],
    }
    digest = sha256_hex(canonical(payload))
    return manifest, payload, digest


def _seal_result(arch, replay):
    return {
        "replay": replay, "sealed": True, "batch_ref": arch["batch_ref"],
        "digest": arch["digest"], "sealed_at": arch["sealed_at"],
        "sealed_by": arch["sealed_by"],
        "receipt_count": arch["receipt_count"],
        "audit_count": arch["audit_count"],
        "freed_bytes": arch["freed_bytes"],
        "manifest": json.loads(arch["manifest"]),
        "message": ("批次此前已封存，返回第一次封存结果（幂等）" if replay
                    else "批次已封存：热库明细已迁入只读归档"),
    }


def seal_batch(db, batch_ref, actor="operator", note=""):
    """执行封存（幂等）。重复调用返回第一次结果；不满足条件/校验失败拒绝。"""
    pkg = _get_pkg(db, batch_ref)
    if pkg["archived"]:
        arch = db.conn.execute("SELECT * FROM archives WHERE batch_ref=?",
                               (batch_ref,)).fetchone()
        if not arch:  # 单事务保证不会发生；防御性报错
            raise RelayError(500, "archive_inconsistent",
                             f"批次 {batch_ref} 标记已封存但归档缺失")
        db.audit("batch_seal_replay", batch_ref, actor=actor,
                 detail={"digest": arch["digest"]})
        db.commit()
        return _seal_result(arch, replay=True)

    reasons = eligibility_reasons(db, pkg)
    if reasons:
        db.audit("batch_seal_rejected", batch_ref, actor=actor,
                 detail={"reasons": reasons})
        db.commit()
        raise RelayError(409, "batch_not_sealable",
                         "批次不满足封存条件（存在缺口/待重试/阻塞项或回执未结束）",
                         {"reasons": reasons})

    receipts = db.conn.execute(
        "SELECT * FROM receipts WHERE batch_ref=? ORDER BY seq", (batch_ref,)).fetchall()
    errs = _integrity_errors(db, pkg, receipts)
    if errs:
        db.audit("batch_seal_rejected", batch_ref, actor=actor,
                 detail={"checksum_errors": errs})
        db.commit()
        raise RelayError(409, "checksum_mismatch",
                         "封存前完整性校验失败：内容哈希与原文不一致",
                         {"errors": errs})

    audit_rows = db.conn.execute(
        "SELECT * FROM audit_log WHERE batch_ref=? ORDER BY id",
        (batch_ref,)).fetchall()
    usage = _hot_usage(db, batch_ref)
    t = now()
    try:
        manifest, payload, digest = _build_archive_payload(
            db, pkg, receipts, audit_rows, t, actor, note)
        db.conn.execute(
            "INSERT INTO archives(batch_ref,package_id,batch_summary,digest,manifest,"
            "payload,receipt_count,audit_count,freed_bytes,sealed_at,sealed_by,note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch_ref, pkg["package_id"], pkg["batch_summary"], digest,
             json.dumps(manifest, ensure_ascii=False),
             json.dumps(payload, ensure_ascii=False),
             len(receipts), len(audit_rows), usage["hot_bytes"], t, actor, note))
        # 热库明细迁入归档：删除回执与该批次审计行（时间线已保存在归档内）
        db.conn.execute("DELETE FROM receipts WHERE batch_ref=?", (batch_ref,))
        db.conn.execute("DELETE FROM audit_log WHERE batch_ref=?", (batch_ref,))
        db.conn.execute(
            "UPDATE packages SET archived=1,archived_at=? WHERE batch_ref=?",
            (t, batch_ref))
        db.audit("batch_sealed", batch_ref, actor=actor,
                 detail={"digest": digest, "receipt_count": len(receipts),
                         "audit_count": len(audit_rows),
                         "freed_bytes": usage["hot_bytes"], "note": note})
        db.commit()
    except Exception:
        db.conn.rollback()  # 不留半迁移状态：归档行/删除/标记同生共死
        raise
    arch = db.conn.execute("SELECT * FROM archives WHERE batch_ref=?",
                           (batch_ref,)).fetchone()
    return _seal_result(arch, replay=False)


# ============================================================ 已封存批次的回执入账

def ingest_into_archived(db, pkg, raw, content_hash, actor="api", commit=True):
    """已封存批次的回执处理：完全相同→归档重放；否则拒绝。

    归档记录永不再次进入转发队列：本函数不触碰 receipts/cursors 热表。
    """
    batch_ref = pkg["batch_ref"]
    arch = db.conn.execute("SELECT digest,manifest FROM archives WHERE batch_ref=?",
                           (batch_ref,)).fetchone()
    manifest = json.loads(arch["manifest"]) if arch else {"receipts": []}
    by_hash = {e["content_hash"]: e for e in manifest.get("receipts", [])}
    hit = by_hash.get(content_hash)
    if hit:
        db.audit("receipt_replay_archived", batch_ref, seq=hit["seq"],
                 receipt_no=hit["receipt_no"], actor=actor,
                 detail={"content_hash": content_hash})
        if commit:
            db.commit()
        return {"seq": hit["seq"], "receipt_no": hit["receipt_no"],
                "state": "archived", "status": "duplicate_replay",
                "result_code": hit.get("result_code", ""),
                "message": "批次已封存；完全相同的回执返回归档中的原结果"
                           "（不进入转发队列）"}
    db.add_conflict("batch_archived",
                    {"_raw": raw, "_content_hash": content_hash,
                     "batch_ref": batch_ref, "seq": raw.get("seq"),
                     "receipt_no": str(raw.get("receipt_no", ""))},
                    "批次已封存，回执不再入账")
    db.audit("receipt_rejected_archived", batch_ref, seq=raw.get("seq"),
             receipt_no=str(raw.get("receipt_no", "")), actor=actor,
             detail={"content_hash": content_hash})
    if commit:
        db.commit()
    raise RelayError(409, "batch_archived",
                     f"批次 {batch_ref} 已封存为只读归档，"
                     "新回执不再进入转发队列",
                     {"digest": arch["digest"] if arch else ""})


# ============================================================ 归档视图 / 校验 / 下载

def list_archives(db):
    return [dict(a) for a in db.conn.execute(
        "SELECT batch_ref,package_id,batch_summary,digest,receipt_count,audit_count,"
        "freed_bytes,sealed_at,sealed_by,note,"
        "LENGTH(CAST(payload AS BLOB)) payload_bytes"
        " FROM archives ORDER BY sealed_at DESC")]


def verify_archive(arch):
    """重算校验摘要并与存储值比对；同时核对清单与归档正文一致。"""
    payload = json.loads(arch["payload"])
    actual = sha256_hex(canonical(payload))
    manifest_ok = canonical(json.loads(arch["manifest"])) == \
        canonical(payload.get("manifest"))
    return {"ok": actual == arch["digest"] and manifest_ok,
            "digest_stored": arch["digest"], "digest_actual": actual,
            "manifest_consistent": manifest_ok, "checked_at": now()}


def _receipt_view_from_archive(e):
    return {
        "seq": e["seq"], "receipt_no": e["receipt_no"], "type": e["type"],
        "type_label": core.TYPE_LABELS.get(e["type"], e["type"]),
        "state": e["state"],
        "state_label": core.STATE_LABELS.get(e["state"], e["state"]),
        "attempts": e["attempts"], "last_error": "",
        "result_code": e["result_code"], "result_detail": e["result_detail"],
        "next_retry_at": None, "lease_until": None,
        "created_at": e["created_at"], "updated_at": e["updated_at"],
        "forwarded_at": e["forwarded_at"], "rejected_at": None,
        "content_hash": e["content_hash"], "archived": True,
        "payload": e.get("payload"),
    }


def get_archive_detail(db, batch_ref, verify=True):
    """校验并恢复只读详情：清单 + 回执明细 + 原审计时间线。

    校验失败（摘要被篡改/不一致）→ 409 archive_corrupt，拒绝恢复。
    """
    arch = _get_archive(db, batch_ref)
    payload = json.loads(arch["payload"])
    verification = verify_archive(arch) if verify else None
    if verification and not verification["ok"]:
        db.audit("archive_verify_failed", batch_ref, detail=verification)
        db.commit()
        raise RelayError(409, "archive_corrupt",
                         "归档校验失败：校验摘要与内容不一致（拒绝恢复）",
                         verification)
    return {
        "batch_ref": batch_ref, "archived": True, "readonly": True,
        "digest": arch["digest"], "sealed_at": arch["sealed_at"],
        "sealed_by": arch["sealed_by"], "note": arch["note"],
        "verification": verification,
        "manifest": json.loads(arch["manifest"]),
        "package": payload.get("package"),
        "receipts": [_receipt_view_from_archive(e)
                     for e in payload.get("receipts", [])],
        "audit": payload.get("audit", []),  # 原审计时间线
    }


def archive_document(db, batch_ref):
    """下载用完整归档文档：去掉附加字段后重算 canonical sha256 即得 digest。"""
    arch = _get_archive(db, batch_ref)
    doc = json.loads(arch["payload"])
    doc["digest"] = arch["digest"]
    doc["sealed_at"] = arch["sealed_at"]
    doc["sealed_by"] = arch["sealed_by"]
    doc["freed_bytes"] = arch["freed_bytes"]
    return doc
