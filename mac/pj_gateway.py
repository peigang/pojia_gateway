#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pj_gateway.py — Claude Code ⇄ 上游 Anthropic 兼容端点的本地网关

功能：
  1. 监听 127.0.0.1，接收 Anthropic Messages API 请求
  2. 转发到 PJ_UPSTREAM（默认 https://api.deepseek.com/anthropic）
  3. system 策略：passthrough / append / replace（内容从文件读，你自己维护）
  4. SSE 流式透传，不缓冲、不攒包
  5. 每次请求落一条 JSONL 日志

仅依赖标准库，Python 3.9+。
"""

import json
import os
import ssl
import sys
import time
import threading
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CFG = {
    "port": int(os.environ.get("PJ_PORT", "8788")),
    "upstream": os.environ.get("PJ_UPSTREAM", "https://api.deepseek.com/anthropic"),
    "api_key": os.environ.get("PJ_API_KEY", "").strip(),
    "system_mode": os.environ.get("PJ_SYSTEM_MODE", "replace").strip().lower(),
    "system_file": os.environ.get("PJ_SYSTEM_FILE", "system_override.txt"),
    "system_append": os.environ.get("PJ_SYSTEM_APPEND", ""),
    "log_path": os.environ.get("PJ_LOG", "gateway.jsonl"),
    "timeout": float(os.environ.get("PJ_TIMEOUT", "600")),
}

LOG_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
STATS = {"requests": 0, "errors": 0, "started": time.time()}

# 本次运行（从网关启动算起）的 token 累计，流式/非流式都统计
USAGE_LOCK = threading.Lock()
USAGE = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "calls": 0}


def reset_usage():
    with USAGE_LOCK:
        for k in USAGE:
            USAGE[k] = 0


def _add_tokens(field, value):
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return
    if n:
        with USAGE_LOCK:
            USAGE[field] += n


def observe_usage(event_type, usage):
    """按 SSE 事件类型累加，避免 message_start 与 message_delta 重复计数。"""
    if not isinstance(usage, dict):
        return
    if event_type == "message_start":
        _add_tokens("input", usage.get("input_tokens"))
        _add_tokens("cache_read", usage.get("cache_read_input_tokens"))
        _add_tokens("cache_creation", usage.get("cache_creation_input_tokens"))
        return
    if event_type == "message_delta":
        _add_tokens("output", usage.get("output_tokens"))
        return
    _add_tokens("input", usage.get("input_tokens"))
    _add_tokens("output", usage.get("output_tokens"))
    _add_tokens("cache_read", usage.get("cache_read_input_tokens"))
    _add_tokens("cache_creation", usage.get("cache_creation_input_tokens"))


def bump_calls():
    with USAGE_LOCK:
        USAGE["calls"] += 1


# ---------------------------------------------------------------- system 策略

def read_system_file():
    path = CFG["system_file"]
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        print(f"[warn] 读 system 文件失败: {exc}", file=sys.stderr)
        return None


def system_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def apply_system(body):
    """按配置改写 body['system']，返回新 body。"""
    mode = CFG["system_mode"]

    if mode == "replace":
        text = read_system_file()
        if text is not None:
            body["system"] = [{"type": "text", "text": text}]
        return body

    if mode != "append":
        return body

    extra = CFG["system_append"]
    if not extra:
        text = read_system_file()
        extra = text if text is not None else ""
    if not extra.strip():
        return body

    current = body.get("system")
    if isinstance(current, list):
        body["system"] = current + [{"type": "text", "text": extra}]
    else:
        joined = (system_text(current) + "\n\n" + extra).strip()
        body["system"] = joined
    return body


# ---------------------------------------------------------------- 工具函数

def log_entry(record):
    try:
        line = json.dumps(record, ensure_ascii=False)
        with LOG_LOCK:
            with open(CFG["log_path"], "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


def bump(key, delta=1):
    with STATS_LOCK:
        STATS[key] = STATS.get(key, 0) + delta


def upstream_parts():
    url = urlsplit(CFG["upstream"])
    scheme = url.scheme or "https"
    host = url.hostname
    port = url.port or (443 if scheme == "https" else 80)
    base_path = (url.path or "").rstrip("/")
    return scheme, host, port, base_path


def join_path(base_path, incoming):
    path = urlsplit(incoming).path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return (base_path + path) or "/"


# ---------------------------------------------------------------- HTTP 处理

class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pj-gateway/1.0"

    def log_message(self, fmt, *args):  # 关闭默认 stderr 噪音
        pass

    # -------- 响应工具

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def err(self, status, err_type, message):
        self.send_json(status, {
            "type": "error",
            "error": {"type": err_type, "message": message},
        })

    # -------- GET

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/__health", "/health", "/"):
            with STATS_LOCK:
                stats = dict(STATS)
            stats["uptime_s"] = int(time.time() - STATS["started"])
            return self.send_json(200, {
                "ok": True,
                "upstream": CFG["upstream"],
                "system_mode": CFG["system_mode"],
                "system_file": CFG["system_file"],
                "system_file_present": os.path.isfile(CFG["system_file"]),
                "stats": stats,
                "usage": {k: USAGE[k] for k in USAGE},
            })
        self.err(404, "not_found_error", f"no route for GET {path}")

    # -------- POST

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.err(400, "invalid_request_error", "bad Content-Length")

        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            return self.err(400, "invalid_request_error", f"body is not JSON: {exc}")

        path = urlsplit(self.path).path.rstrip("/")
        is_messages = path.endswith("/v1/messages") or path.endswith("/messages")
        system_in_chars = 0
        if is_messages:
            # 客户端原本发了多长的系统提示（用于界面估算 append 模式的总量）
            system_in_chars = len(system_text(body.get("system")))
            body = apply_system(body)

        self.relay(body, is_messages, system_in_chars)

    # -------- 转发核心

    def relay(self, body, is_messages, system_in_chars=0):
        scheme, host, port, base_path = upstream_parts()

        # 自环保护：上游指回本网关时，请求会无限转发直到端口耗尽（WinError 10048）
        if host in ("127.0.0.1", "localhost", "::1") and int(port) == int(CFG["port"]):
            msg = ("上游地址指向本网关自身（%s:%s），请求会自我循环。"
                   "请把上游改成 cc-switch 的代理端口，通常是 http://127.0.0.1:15721。"
                   % (host, port))
            self.err(502, "api_error", msg)
            log_entry({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "path": self.path,
                "upstream": f"{scheme}://{host}:{port}{base_path}",
                "model": body.get("model"),
                "stream": bool(body.get("stream")),
                "messages": len(body.get("messages") or []),
                "tools": len(body.get("tools") or []),
                "system_chars": len(system_text(body.get("system"))),
                "system_in_chars": system_in_chars,
                "system_mode": CFG["system_mode"],
                "status": 502,
                "ms": 0,
                "note": "self-loop",
            })
            bump("errors")
            return

        target = join_path(base_path, self.path)
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")

        want_stream = bool(body.get("stream"))
        accept = self.headers.get("Accept", "")
        if "text/event-stream" in accept:
            want_stream = True

        headers = self.build_headers(host, payload)

        started = time.time()
        status = 502
        note = ""
        in_bytes = 0
        out_tokens = None

        conn = None
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(host, port, timeout=CFG["timeout"], context=ctx)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=CFG["timeout"])

            conn.request("POST", target, body=payload, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            ctype = resp.getheader("Content-Type", "application/json")

            if want_stream and status < 400:
                self.pump_stream(resp, status, ctype)
            else:
                data = resp.read()
                out_tokens = peek_usage(data)
                observe_usage("", out_tokens)
                self.pump_bulk(resp, status, ctype, data)

            if status < 400:
                bump_calls()
            bump("requests")
        except (BrokenPipeError, ConnectionResetError):
            note = "client disconnected"
            bump("errors")
        except Exception as exc:  # noqa: BLE001
            note = f"{type(exc).__name__}: {exc}"
            bump("errors")
            if status == 502:
                try:
                    self.err(502, "api_error", f"upstream failure: {note}")
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "path": self.path,
            "upstream": target,
            "model": body.get("model"),
            "stream": want_stream,
            "messages": len(body.get("messages") or []),
            "tools": len(body.get("tools") or []),
            "system_chars": len(system_text(body.get("system"))),
            "system_in_chars": system_in_chars,
            "system_mode": CFG["system_mode"],
            "status": status,
            "ms": int((time.time() - started) * 1000),
            "note": note,
        }
        if out_tokens is not None:
            record["usage"] = out_tokens
        log_entry(record)

    def build_headers(self, host, payload):
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "Accept": self.headers.get("Accept", "application/json"),
            "User-Agent": self.headers.get("User-Agent", "pj-gateway/1.0"),
        }
        for name in ("anthropic-version", "anthropic-beta"):
            value = self.headers.get(name)
            if value:
                headers[name] = value
        if "anthropic-version" not in headers:
            headers["anthropic-version"] = "2023-06-01"

        key = CFG["api_key"] or (self.headers.get("x-api-key") or "").strip()
        auth = (self.headers.get("Authorization") or "").strip()
        if key:
            headers["x-api-key"] = key
            headers["Authorization"] = f"Bearer {key}"
        elif auth:
            headers["Authorization"] = auth
        return headers

    def pump_stream(self, resp, status, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype or "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        buf = bytearray()
        while True:
            chunk = resp.read1(8192) if hasattr(resp, "read1") else resp.read(8192)
            if not chunk:
                if getattr(resp, "isclosed", lambda: True)():
                    break
                continue
            self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
            self.wfile.flush()
            self._tap_sse(chunk, buf)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    @staticmethod
    def _tap_sse(chunk, buf):
        """旁路解析 SSE，累计 usage；不改动转发内容。"""
        buf.extend(chunk)
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(buf[:idx]).strip()
            del buf[:idx + 1]
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(obj, dict):
                continue
            usage = obj.get("usage")
            if not isinstance(usage, dict):
                msg = obj.get("message")
                usage = msg.get("usage") if isinstance(msg, dict) else None
            observe_usage(obj.get("type", ""), usage)
        if len(buf) > 1 << 20:      # 异常长的单行，丢弃防止内存膨胀
            del buf[:]

    def pump_bulk(self, resp, status, ctype, data):
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/json")
        for name in ("request-id", "anthropic-ratelimit-requests-remaining"):
            value = resp.getheader(name)
            if value:
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def peek_usage(data):
    try:
        obj = json.loads(data)
    except Exception:  # noqa: BLE001
        return None
    usage = obj.get("usage") if isinstance(obj, dict) else None
    return usage if isinstance(usage, dict) else None


# ---------------------------------------------------------------- 启动

def main():
    scheme, host, port, base_path = upstream_parts()
    print("=" * 62)
    print("pj_gateway  Anthropic 兼容网关")
    print(f"  监听      http://127.0.0.1:{CFG['port']}")
    print(f"  上游      {scheme}://{host}:{port}{base_path}")
    print(f"  system    mode={CFG['system_mode']}  file={CFG['system_file']} "
          f"({'存在' if os.path.isfile(CFG['system_file']) else '缺失'})")
    print(f"  日志      {CFG['log_path']}")
    print(f"  鉴权      {'PJ_API_KEY 覆盖' if CFG['api_key'] else '透传客户端头'}")
    print("=" * 62)
    if CFG["system_mode"] not in ("passthrough", "append", "replace"):
        print(f"[warn] 未知 PJ_SYSTEM_MODE={CFG['system_mode']}，按 passthrough 处理",
              file=sys.stderr)

    server = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), GatewayHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[exit]")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
