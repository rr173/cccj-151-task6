"""离线车队回执中继 —— HTTP 服务（Python 标准库，零第三方依赖）。

线程模型：ThreadingHTTPServer 接收请求；全部读写事务在 db 全局锁内串行，
后台 Forwarder 共用同一把锁（见 forwarder.py 的两阶段租约）。

API 总览（详见 relay/API.md）：
  管理侧  POST /relay/api/packages/import          执行包导入（幂等/不可变）
          GET  /relay/api/batches                   批次列表（缺口/缓冲/转发状态）
          GET  /relay/api/batches/{ref}             批次详情（含逐条回执与失败原因）
          POST /relay/api/batches/{ref}/receipts    批量回执上传
          POST /relay/api/receipts                  单条回执上传
          GET  /relay/api/queue                     待转发/阻塞队列
          POST /relay/api/retries                   失败重试
          POST /relay/api/batches/{ref}/receipts/{seq}/ack-terminal
          GET  /relay/api/audit                     审计流
          GET  /relay/api/conflicts                 被拒冲突
  封存    GET  /relay/api/admin/storage             热库占用预估与封存资格
          POST /relay/api/batches/{ref}/seal        执行封存（幂等，单事务）
          GET  /relay/api/archives                  只读归档列表
          GET  /relay/api/archives/{ref}            校验并恢复只读详情（含原审计时间线）
          GET  /relay/api/archives/{ref}/download   下载归档 JSON（可离线校验）
  现场终端 GET /relay/api/terminal/manifest          可下载批次清单
          GET  /relay/api/terminal/packages/{ref}   下载不可变执行包（含摘要）
  运维    GET  /relay/api/health
          POST /relay/api/admin/stub                stub 适配器规则注入
          POST /relay/api/admin/recover             手动触发重启恢复逻辑
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

import archive
import approvals
import core
from adapters import build_adapter
from db import RelayDB, ROLE_LABELS, ACTION_ROLES, ACTION_LABELS
from forwarder import Forwarder

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")
DB_PATH = os.environ.get(
    "RELAY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "relay.db"))


class RelayApp:
    def __init__(self, db_path=None, adapter=None, auto_start=True,
                 enforce_approvals=None, **fw_kwargs):
        self.db = RelayDB(db_path or DB_PATH)
        self.adapter = adapter or build_adapter()
        # 敏感指令是否强制双人复核（默认开启；老测试经工厂关闭）
        if enforce_approvals is None:
            enforce_approvals = os.environ.get(
                "RELAY_ENFORCE_APPROVALS", "1").strip().lower() not in ("0", "false", "no")
        self.enforce_approvals = bool(enforce_approvals)
        # 账号管理令牌：设置后管理账号需 X-Relay-Admin-Token；不设则本地演示开放
        self.admin_token = os.environ.get("RELAY_ADMIN_TOKEN", "")
        # 任何资源变化提交前，让目标状态已变化的未决事项立即失效
        self.db.precommit.append(approvals.invalidate_on_precommit)
        approvals.seed_default_accounts(self.db)
        self.forwarder = Forwarder(self.db, self.adapter, **fw_kwargs)
        rec = core.recover_on_startup(self.db)
        rec["expired_approvals"] = approvals.expire_pending(self.db,
                                                            sweep_approved=True)
        if auto_start:
            self.forwarder.start()
            self.forwarder.kick()
        self.startup = rec


# 单进程默认实例（main 与测试工厂均可用 build_app 覆盖）
APP = None


def build_app(**kwargs):
    global APP
    APP = RelayApp(**kwargs)
    return APP


def app():
    global APP
    if APP is None:
        APP = RelayApp()
    return APP


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "RelayHTTP/1.0"

    def log_message(self, fmt, *args):
        print("[relay] " + fmt % args)

    # ---- 输出工具 ----
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
        except (ValueError, UnicodeDecodeError):
            self._bad_json()

    def _bad_json(self):
        raise core.RelayError(400, "malformed", "请求体不是合法 JSON")

    # ---- 身份与岗位 ----
    def _username(self, body=None):
        u = self.headers.get("X-Relay-User", "")
        if not u and isinstance(body, dict):
            u = str(body.get("_user", "") or "")
        return str(u).strip()

    def _current_user(self, a, body=None, required=True):
        u = self._username(body)
        if not u:
            if required:
                raise core.RelayError(401, "unauthenticated",
                                      "缺少值班身份：请通过 X-Relay-User 请求头"
                                      "或 _user 字段指定账号")
            return None, u
        return approvals.require_account(a.db, u), u

    def _require_observe(self, a, body=None):
        # 关闭复核的兼容模式（老测试/种子直连）不强制登录
        if not a.enforce_approvals:
            return None, self._username(body)
        row, u = self._current_user(a, body)
        approvals.require_role(row, "observe")
        return row, u

    def _check_admin_token(self, a):
        if a.admin_token:
            token = self.headers.get("X-Relay-Admin-Token", "")
            if token != a.admin_token:
                raise core.RelayError(403, "admin_token_required",
                                      "账号管理需要有效的 X-Relay-Admin-Token")

    def _download(self, filename, obj):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition",
                         f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        if path in ("", "/"):
            path = "index.html"
        base = os.path.abspath(FRONTEND_DIR)
        safe = os.path.normpath(os.path.join(base, path.lstrip("/")))
        if not safe.startswith(base + os.sep) or not os.path.isfile(safe):
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8",
                 ".svg": "image/svg+xml",
                 ".json": "application/json; charset=utf-8"}.get(
            os.path.splitext(safe)[1], "application/octet-stream")
        with open(safe, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if safe.endswith("index.html"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---- 路由 ----
    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p == "/":
                self.send_response(302)
                self.send_header("Location", "/relay/")
                self.end_headers()
                return
            if p.startswith("/relay/api/") or p == "/relay/api":
                return self._route_get(p)
            if p.startswith("/relay/"):
                return self._static(p[len("/relay/"):])
            if p == "/relay":
                return self._static("index.html")
            self.send_error(404)
        except core.RelayError as e:
            self._json(e.status, {"error": e.message, "code": e.code, **e.extra})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"服务器内部错误: {e}"})

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self._read_body()
            if p.startswith("/relay/api/") or p == "/relay/api":
                return self._route_post(p, body)
            self.send_error(404)
        except core.RelayError as e:
            self._json(e.status, {"error": e.message, "code": e.code, **e.extra})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"服务器内部错误: {e}"})

    def _route_get(self, p):
        a = app()
        with a.db.lock:
            approvals.expire_pending(a.db)
            if p == "/relay/api/health":
                return self._json(200, {"ok": True, "adapter": a.adapter.name,
                                        "forwarder": a.forwarder.stats,
                                        "startup": a.startup,
                                        "enforce_approvals": a.enforce_approvals})
            if p == "/relay/api/me":
                row, u = self._current_user(a, required=False)
                if not row:
                    return self._json(200, {"authenticated": False,
                                            "accounts": [x["username"]
                                                         for x in approvals.list_accounts(a.db)]})
                d = dict(row)
                d["role_list"] = sorted(approvals.account_roles(row))
                d["role_labels"] = [ROLE_LABELS.get(x, x) for x in d["role_list"]]
                d["authenticated"] = True
                return self._json(200, d)
            if p == "/relay/api/accounts":
                self._check_admin_token(a)
                return self._json(200, {"accounts": approvals.list_accounts(a.db)})

            # 以下管理接口均需「观察」权限（现场终端设备接口另行放行；兼容模式不强制）
            if not (p == "/relay/api/terminal/manifest"
                    or p.startswith("/relay/api/terminal/packages/")):
                self._require_observe(a)

            if p == "/relay/api/batches":
                u = self._username()
                return self._json(200, {
                    "batches": core.list_batches(a.db),
                    "approvals": {"pending": len(approvals.list_approvals(
                        a.db, status="pending")),
                                  "my_todo": (approvals.todo_for(a.db, u)["total"]
                                              if u else 0)}})
            if p == "/relay/api/queue":
                return self._json(200, core.queue_overview(a.db))
            if p == "/relay/api/audit":
                return self._json(200, {"events": core.recent_audit(a.db)})
            if p == "/relay/api/conflicts":
                return self._json(200, {"conflicts": core.recent_conflicts(a.db)})

            if p == "/relay/api/approvals/todo":
                _, u = self._current_user(a)
                return self._json(200, approvals.todo_for(a.db, u))
            if p == "/relay/api/approval-decisions":
                return self._json(200, {"decisions": approvals.decisions(a.db)})
            if p.startswith("/relay/api/approvals/"):
                parts = p.strip("/").split("/")
                if len(parts) == 4:
                    row = approvals._get_pending(a.db, parts[3])
                    view = approvals._view_by_row(a.db, row, viewer=self._username())
                    view["decisions"] = approvals.decisions(a.db, parts[3])
                    return self._json(200, view)
            if p == "/relay/api/approvals":
                qs = parse_qs(urlparse(self.path).query)
                status = qs.get("status", [None])[0]
                if status:
                    status = status.split(",")
                viewer = self._username()
                only_mine = qs.get("mine", ["0"])[0] == "1"
                items = approvals.list_approvals(
                    a.db, viewer=viewer or None, status=status,
                    requested_by=viewer if only_mine else None)
                return self._json(200, {"approvals": items})

            if p == "/relay/api/admin/storage":
                return self._json(200, archive.estimate_storage(a.db))
            if p == "/relay/api/archives":
                return self._json(200, {"archives": archive.list_archives(a.db)})
            if p.startswith("/relay/api/archives/"):
                parts = p.strip("/").split("/")
                ref = parts[3]
                if len(parts) == 4:
                    return self._json(200, archive.get_archive_detail(a.db, ref))
                if len(parts) == 5 and parts[4] == "download":
                    doc = archive.archive_document(a.db, ref)
                    return self._download(f"relay-archive-{ref}.json", doc)

            if p.startswith("/relay/api/batches/"):
                parts = p.strip("/").split("/")
                ref = parts[3]
                if len(parts) == 4:
                    return self._json(200, core.get_batch_detail(a.db, ref))

            if p == "/relay/api/terminal/manifest":
                return self._json(200, {"batches": _terminal_manifest(a.db)})
            if p.startswith("/relay/api/terminal/packages/"):
                ref = p.rsplit("/", 1)[-1]
                row = a.db.conn.execute(
                    "SELECT payload,content_hash,batch_summary,imported_at FROM packages"
                    " WHERE batch_ref=?", (ref,)).fetchone()
                if not row:
                    raise core.RelayError(404, "not_found", f"批次 {ref} 不存在")
                pkg = json.loads(row["payload"])
                pkg.setdefault("relay", {})
                pkg["relay"] = {"content_hash": row["content_hash"],
                                "imported_at": row["imported_at"],
                                "immutable": True}
                return self._json(200, pkg)
            raise core.RelayError(404, "not_found", "未知接口")

    def _route_post(self, p, body):
        a = app()
        body = body if isinstance(body, dict) else {}

        # ---- 值班账号管理（设置了 admin token 时需 X-Relay-Admin-Token）----
        if p == "/relay/api/accounts" or (
                p.startswith("/relay/api/accounts/") and p.endswith("/active")):
            with a.db.lock:
                self._check_admin_token(a)
                _, u = self._current_user(a, body)
                if p == "/relay/api/accounts":
                    res = approvals.upsert_account(
                        a.db, str(body.get("username", "")),
                        display_name=body.get("display_name"),
                        roles=body.get("roles"), active=body.get("active"),
                        note=body.get("note"), actor=u or "admin")
                else:
                    uname = p.strip("/").split("/")[3]
                    res = approvals.set_active(a.db, uname, bool(body.get("active")),
                                               actor=u or "admin")
                return self._json(200, res)

        # ---- 双人复核：申请 / 批准或拒绝 ----
        if p == "/relay/api/approvals":
            with a.db.lock:
                approvals.expire_pending(a.db)
                _, u = self._current_user(a, body)
                res = approvals.request_approval(a.db, body, u)
                a.forwarder.kick()
                return self._json(201 if res.get("status") == "pending" and not
                                  res.get("idempotent_replay") else 200, res)
        if p.startswith("/relay/api/approvals/") and p.endswith("/decide"):
            with a.db.lock:
                approvals.expire_pending(a.db)
                _, u = self._current_user(a, body)
                ap_id = p.strip("/").split("/")[3]
                res = approvals.decide_approval(
                    a.db, ap_id, u, str(body.get("decision", "approve")),
                    note=str(body.get("note", "")))
                a.forwarder.kick()
                return self._json(200, res)

        actor = str(body.get("_actor", "api"))
        with a.db.lock:
            approvals.expire_pending(a.db)
            if p == "/relay/api/packages/import":
                self._gate_sensitive(a, body, "package_import")
                res = core.import_package(a.db, body.get("package", body),
                                          actor=actor, source=str(body.get("source", "api")))
                a.forwarder.kick()
                status = 200 if res.get("replay") else 201
                return self._json(status, res)

            if p == "/relay/api/receipts":
                self._require_observe(a, body)
                res = core.ingest_receipt(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/receipts"):
                self._require_observe(a, body)
                ref = p.strip("/").split("/")[3]
                body = dict(body)
                body.setdefault("batch_ref", ref)
                res = core.ingest_batch(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)
            if p == "/relay/api/receipts/batch":
                self._require_observe(a, body)
                res = core.ingest_batch(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)

            if p == "/relay/api/retries":
                self._gate_sensitive(a, body, "manual_retry")
                res = core.manual_retry(a.db, str(body.get("batch_ref", "")),
                                        seq=body.get("seq"), actor=actor,
                                        note=str(body.get("note", "")))
                a.forwarder.kick()
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/seal"):
                self._gate_sensitive(a, body, "batch_seal")
                ref = p.strip("/").split("/")[3]
                res = archive.seal_batch(a.db, ref, actor=actor,
                                         note=str(body.get("note", "")))
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/ack-terminal"):
                self._gate_sensitive(a, body, "ack_terminal")
                parts = p.strip("/").split("/")
                ref, seq = parts[3], int(parts[5])
                res = core.acknowledge_and_retry(
                    a.db, ref, seq, actor=actor, note=str(body.get("note", "")))
                a.forwarder.kick()
                return self._json(200, res)

            if p == "/relay/api/admin/stub":
                self._require_observe(a, body)
                if a.adapter.name != "stub":
                    raise core.RelayError(409, "adapter_not_stub",
                                          f"当前适配器为 {a.adapter.name}，不可注入桩规则")
                a.adapter.configure(rules=body.get("rules"),
                                    reset_calls=bool(body.get("reset_calls")))
                a.db.audit("stub_rules_updated", actor=actor,
                           detail={"rules": body.get("rules")})
                a.db.commit()
                a.forwarder.kick()
                return self._json(200, a.adapter.snapshot())

            if p == "/relay/api/admin/state":
                snap = a.adapter.snapshot() if hasattr(a.adapter, "snapshot") else None
                return self._json(200, {"adapter": a.adapter.name, "stub": snap,
                                        "forwarder": a.forwarder.stats})

            if p == "/relay/api/admin/recover":
                return self._json(200, core.recover_on_startup(a.db))

            raise core.RelayError(404, "not_found", "未知接口")

    def _gate_sensitive(self, a, body, action):
        """强制双人复核时，敏感指令必须经 /approvals 批准后由复核动作内部调用。

        带 _bypass_approval=True 的调用来自批准执行通道（仍需有效观察身份）；
        其余直调在 enforce_approvals 开启时一律 403 并提示申请路径。
        关闭复核（老测试/兼容模式）时仅校验申请人岗位权限。
        """
        if body.get("_bypass_approval"):
            self._require_observe(a, body)
            return
        if not a.enforce_approvals:
            return  # 兼容模式（老测试/种子直连）：保持原直调行为
        _, u = self._current_user(a, body)
        row = approvals.require_account(a.db, u)
        approvals.require_role(row, ACTION_ROLES[action])
        if a.enforce_approvals:
            raise core.RelayError(
                403, "approval_required",
                f"敏感指令「{ACTION_LABELS[action]}」必须双人复核：先 "
                "POST /relay/api/approvals 提交申请，再由另一个具备权限的账号调用 "
                "POST /relay/api/approvals/{id}/decide 批准后才会执行",
                {"action": action, "required_role": ACTION_ROLES[action],
                 "apply_endpoint": "/relay/api/approvals",
                 "decide_endpoint_pattern": "/relay/api/approvals/{id}/decide"})


def _terminal_manifest(a_db):
    out = []
    for p in a_db.conn.execute(
            "SELECT batch_ref,package_id,batch_summary,source,imported_at,"
            "content_hash,archived FROM packages ORDER BY imported_at"):
        d = dict(p)
        if d.get("archived"):
            # 已封存：热库明细已迁入归档，已转发数取归档清单
            arch = a_db.conn.execute(
                "SELECT receipt_count FROM archives WHERE batch_ref=?",
                (p["batch_ref"],)).fetchone()
            d["forwarded_receipts"] = arch["receipt_count"] if arch else 0
        else:
            cur = a_db.conn.execute(
                "SELECT COUNT(*) c FROM receipts WHERE batch_ref=? AND state='forwarded'",
                (p["batch_ref"],)).fetchone()
            d["forwarded_receipts"] = cur["c"]
        d["download"] = f"/relay/api/terminal/packages/{p['batch_ref']}"
        out.append(d)
    return out


def main():
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8090"))
    a = build_app()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"离线车队回执中继已启动: http://{host}:{port}/relay/")
    print(f"数据库: {os.path.abspath(a.db.path)}  回调适配器: {a.adapter.name}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        a.forwarder.stop()


if __name__ == "__main__":
    main()
