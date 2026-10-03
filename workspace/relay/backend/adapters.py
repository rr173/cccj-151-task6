"""主系统回调适配器（可替换）。

中继通过适配器把按序就绪的回执转发给编排系统。替换实现只需继承 CallbackAdapter
并实现 deliver()，然后在启动时用 RELAY_CALLBACK_ADAPTER 环境变量选择：

    RELAY_CALLBACK_ADAPTER=http   → HTTP 回调真实主系统（默认生产形态）
    RELAY_CALLBACK_ADAPTER=stub   → 进程内桩（离线演示/测试，可注入失败与终态拒绝）

deliver() 必须返回 CallbackResult：
    ack      —— 主系统确认受理（游标才可前推，该回执不再被调用，恰好一次效果）
    retry    —— 超时/网络/5xx/429 等瞬时失败：按指数退避安全重试
    terminal —— 主系统明确终态拒绝：批次已撤回 / 车辆已变化 / 目的地已变化，
                中继冻结该批次游标，后续回执不得越过

适配器需要支持**幂等重放**：中继重试时携带稳定的 idempotency_key（batch_ref:seq:hash），
主系统重复收到同一键必须返回首次结果，而不是重复处理。
"""
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass

# 主系统语义化终态拒绝原因（与主系统回调契约一致）
TERMINAL_REASONS = ("batch_withdrawn", "vehicle_changed", "destination_changed")
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class CallbackResult:
    kind: str                 # ack | retry | terminal
    code: str = ""            # ack / 具体拒绝原因 / timeout | http_503 | network_error ...
    detail: str = ""          # 响应留痕（页面"失败原因/转发结果"展示）
    http_status: int = 0


class CallbackAdapter:
    name = "base"

    def deliver(self, package, receipt, idempotency_key):
        raise NotImplementedError


class StubCallbackAdapter(CallbackAdapter):
    """进程内桩适配器：不访问网络，行为可按 batch_ref:seq 注入。

    规则 key 形如 "B-001:3"（也可用 "B-001:*" 或 "*"），value：
      ack | timeout | network_error | http_500 | http_503 |
      batch_withdrawn | vehicle_changed | destination_changed |
      fail_then_ok        —— 前 N 次瞬时失败，随后成功（N 由规则值 fail_then_ok:N 或
                            RELAY_STUB_FAIL_N 环境变量指定，默认 2）
    """

    name = "stub"

    def __init__(self):
        self.rules = {}
        self.calls = []          # 最近调用记录（供测试与运维页面断言）
        self.max_calls_keep = 500
        self.fail_n = int(os.environ.get("RELAY_STUB_FAIL_N", "2"))

    def configure(self, rules=None, reset_calls=False):
        if rules is not None:
            if not isinstance(rules, dict):
                raise ValueError("rules 必须是对象")
            self.rules = dict(rules)
        if reset_calls:
            self.calls = []

    def snapshot(self):
        return {"rules": dict(self.rules), "calls": list(self.calls[-100:])}

    def _rule_for(self, batch_ref, seq):
        for key in (f"{batch_ref}:{seq}", f"{batch_ref}:*", "*"):
            if key in self.rules:
                return self.rules[key]
        return "ack"

    def deliver(self, package, receipt, idempotency_key):
        batch_ref = receipt["batch_ref"]
        seq = receipt["seq"]
        rule = self._rule_for(batch_ref, seq)
        # 规则值可直接带次数：fail_then_ok:3
        fail_n = self.fail_n
        if isinstance(rule, str) and rule.startswith("fail_then_ok") and ":" in rule:
            try:
                fail_n = int(rule.rsplit(":", 1)[1])
                rule = "fail_then_ok"
            except ValueError:
                pass
        self.calls.append({
            "idempotency_key": idempotency_key, "batch_ref": batch_ref, "seq": seq,
            "receipt_no": receipt["receipt_no"], "type": receipt["receipt_type"],
            "rule": rule, "attempt_hint": len([c for c in self.calls
                                               if c["idempotency_key"] == idempotency_key]),
        })
        if len(self.calls) > self.max_calls_keep:
            self.calls = self.calls[-self.max_calls_keep:]

        if rule == "ack":
            return CallbackResult("ack", "ack", "stub: 主系统确认受理（模拟）", 200)
        if rule in TERMINAL_REASONS:
            label = {"batch_withdrawn": "批次已撤回",
                     "vehicle_changed": "车辆已变化",
                     "destination_changed": "目的地已变化"}[rule]
            return CallbackResult("terminal", rule, f"stub: {label}（终态拒绝）", 200)
        if rule == "fail_then_ok":
            n_so_far = sum(1 for c in self.calls if c["idempotency_key"] == idempotency_key)
            if n_so_far <= fail_n:
                return CallbackResult("retry", "stub_transient",
                                      f"stub: 第 {n_so_far} 次瞬时失败（共 {fail_n} 次后成功）", 503)
            return CallbackResult("ack", "ack", "stub: 重试后主系统确认受理", 200)
        if rule.startswith("http_"):
            try:
                status = int(rule.split("_", 1)[1])
            except ValueError:
                status = 503
            return CallbackResult("retry", f"http_{status}",
                                  f"stub: 模拟 HTTP {status}", status)
        if rule == "timeout":
            return CallbackResult("retry", "timeout", "stub: 模拟回调超时", 0)
        return CallbackResult("retry", "network_error",
                              f"stub: 模拟网络错误（规则 {rule}）", 0)


class HTTPCallbackAdapter(CallbackAdapter):
    """HTTP 回调适配器：把回执 POST 到主系统的统一回调端点。

    目标：RELAY_CALLBACK_URL（例：http://evac:8080/api/relay-callback）。
    超时：RELAY_CALLBACK_TIMEOUT 秒（默认 8），超时与连接错误均为可安全重试的瞬时失败。
    """

    name = "http"

    def __init__(self, url=None, timeout=None):
        self.url = url or os.environ.get(
            "RELAY_CALLBACK_URL", "http://127.0.0.1:8080/api/relay-callback")
        self.timeout = float(timeout if timeout is not None
                             else os.environ.get("RELAY_CALLBACK_TIMEOUT", "8"))

    def deliver(self, package, receipt, idempotency_key):
        body = {
            "idempotency_key": idempotency_key,
            "batch_ref": receipt["batch_ref"],
            "seq": receipt["seq"],
            "receipt_no": receipt["receipt_no"],
            "type": receipt["receipt_type"],
            "batch_summary": package["batch_summary"],
            "at": receipt.get("ts", ""),
            "payload": receipt,
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Idempotency-Key": idempotency_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            reason = "timeout" if isinstance(e, socket.timeout) else "network_error"
            return CallbackResult("retry", reason, f"回调不可达：{e}", 0)

        parsed, parsed_detail = None, raw
        try:
            parsed = json.loads(raw)
            parsed_detail = json.dumps(parsed, ensure_ascii=False)[:2000]
        except (ValueError, TypeError):
            pass

        if status in TRANSIENT_HTTP:
            return CallbackResult("retry", f"http_{status}",
                                  f"主系统返回 {status}，可重试：{parsed_detail[:300]}", status)
        if status != 200 and not (200 <= status < 300):
            # 非瞬时 4xx：若主系统给出了语义化终态原因则终态拒绝，否则按终态失败处理
            reason = (parsed or {}).get("reason") if isinstance(parsed, dict) else None
            if reason in TERMINAL_REASONS:
                return CallbackResult("terminal", reason, parsed_detail, status)
            return CallbackResult("terminal", f"http_{status}",
                                  f"主系统返回 {status}（非可重试）：{parsed_detail[:300]}", status)

        if isinstance(parsed, dict) and parsed.get("result") == "rejected":
            reason = parsed.get("reason", "rejected")
            kind = "terminal" if reason in TERMINAL_REASONS else "terminal"
            return CallbackResult(kind, reason,
                                  parsed.get("message", parsed_detail), status)
        return CallbackResult("ack", "ack", f"主系统确认受理：{parsed_detail[:300]}", status)


def build_adapter():
    kind = os.environ.get("RELAY_CALLBACK_ADAPTER", "stub").strip().lower()
    if kind == "http":
        return HTTPCallbackAdapter()
    if kind == "stub":
        return StubCallbackAdapter()
    raise RuntimeError(f"未知回调适配器: {kind}（支持 stub / http）")
