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
from urllib.parse import urlparse

import archive
import core
from adapters import build_adapter
from db import RelayDB
from forwarder import Forwarder

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")
DB_PATH = os.environ.get(
    "RELAY_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "relay.db"))


class RelayApp:
    def __init__(self, db_path=None, adapter=None, auto_start=True, **fw_kwargs):
        self.db = RelayDB(db_path or DB_PATH)
        self.adapter = adapter or build_adapter()
        self.forwarder = Forwarder(self.db, self.adapter, **fw_kwargs)
        rec = core.recover_on_startup(self.db)
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
            if p == "/relay/api/health":
                return self._json(200, {"ok": True, "adapter": a.adapter.name,
                                        "forwarder": a.forwarder.stats,
                                        "startup": a.startup})
            if p == "/relay/api/batches":
                return self._json(200, {"batches": core.list_batches(a.db)})
            if p == "/relay/api/queue":
                return self._json(200, core.queue_overview(a.db))
            if p == "/relay/api/audit":
                return self._json(200, {"events": core.recent_audit(a.db)})
            if p == "/relay/api/conflicts":
                return self._json(200, {"conflicts": core.recent_conflicts(a.db)})

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
        actor = str(body.get("_actor", "api"))
        with a.db.lock:
            if p == "/relay/api/packages/import":
                res = core.import_package(a.db, body.get("package", body),
                                          actor=actor, source=str(body.get("source", "api")))
                a.forwarder.kick()
                status = 200 if res.get("replay") else 201
                return self._json(status, res)

            if p == "/relay/api/receipts":
                res = core.ingest_receipt(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/receipts"):
                ref = p.strip("/").split("/")[3]
                body = dict(body)
                body.setdefault("batch_ref", ref)
                res = core.ingest_batch(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)
            if p == "/relay/api/receipts/batch":
                res = core.ingest_batch(a.db, body, actor=actor)
                a.forwarder.kick()
                return self._json(200, res)

            if p == "/relay/api/retries":
                res = core.manual_retry(a.db, str(body.get("batch_ref", "")),
                                        seq=body.get("seq"), actor=actor,
                                        note=str(body.get("note", "")))
                a.forwarder.kick()
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/seal"):
                ref = p.strip("/").split("/")[3]
                res = archive.seal_batch(a.db, ref, actor=actor,
                                         note=str(body.get("note", "")))
                return self._json(200, res)

            if p.startswith("/relay/api/batches/") and p.endswith("/ack-terminal"):
                parts = p.strip("/").split("/")
                ref, seq = parts[3], int(parts[5])
                res = core.acknowledge_and_retry(
                    a.db, ref, seq, actor=actor, note=str(body.get("note", "")))
                a.forwarder.kick()
                return self._json(200, res)

            if p == "/relay/api/admin/stub":
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
