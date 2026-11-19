#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PJ Gateway Studio — pj_gateway 的 Windows 图形化控制台

对接方式（重要）：
  VS Code 的 Claude Code 扩展读 ~/.claude/settings.json，
  cc-switch 会把该文件的 ANTHROPIC_BASE_URL 写成它自己的本地代理（如 127.0.0.1:15721）。
  本程序的做法是插在 Claude Code 与 cc-switch 之间：

      VS Code Claude Code
            │  ANTHROPIC_BASE_URL = http://127.0.0.1:8788
            ▼
      pj_gateway（本程序，注入/替换 system）
            │  upstream = http://127.0.0.1:15721
            ▼
      cc-switch 本地代理 ──► 上游供应商

  因此「接管」= 把 settings.json 的 BASE_URL 指向本网关；
  cc-switch 重新 attach 时会覆盖，所以带自动重接管守卫。
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

import pj_gateway
import pj_persona

APP_NAME = "PJ Gateway Studio"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
UIFONT = "Menlo" if IS_MAC else "Consolas"


def _app_dir():
    """数据目录：Windows 冻结版放 exe 同级；macOS 的 .app 不能写进 bundle，放用户目录。"""
    if getattr(sys, "frozen", False):
        if IS_MAC:
            d = os.path.join(os.path.expanduser("~"), "Library",
                             "Application Support", "PJGateway")
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass
            return d
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


APP_DIR = _app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CLAUDE_SETTINGS = os.path.join(CLAUDE_DIR, "settings.json")
CLAUDE_BACKUP = os.path.join(CLAUDE_DIR, "settings.pj-backup.json")
CCSWITCH_DIR = os.path.join(HOME, ".cc-switch")
CCSWITCH_STATE = os.path.join(CCSWITCH_DIR, "live-state.json")

DEFAULTS = {
    "port": 8788,
    "upstream": "http://127.0.0.1:15721",
    "api_key": "",
    "model": "deepseek-flash",
    "system_mode": "replace",
    "system_file": "system_override.txt",
    "system_append": "",
    "log_path": "gateway.jsonl",
    "timeout": 600.0,
    "takeover": {
        "active": False,
        "guard": True,
        "original": "",
        "gateway_url": "",
        "backup": "",
    },
}

SYSTEM_MODES = ("passthrough", "append", "replace")


# ------------------------------------------------------------------ 通用工具

def abspath(name):
    if not name:
        return ""
    return name if os.path.isabs(name) else os.path.join(APP_DIR, name)


def load_config():
    cfg = dict(DEFAULTS)
    cfg["takeover"] = dict(DEFAULTS["takeover"])
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data.get("takeover"), dict):
                merged = dict(DEFAULTS["takeover"])
                merged.update(data["takeover"])
                data["takeover"] = merged
            cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] config.json 读取失败，使用默认值: {exc}", file=sys.stderr)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def apply_to_engine(cfg):
    pj_gateway.CFG.update({
        "port": int(cfg["port"]),
        "upstream": str(cfg["upstream"]).strip(),
        "api_key": str(cfg["api_key"]).strip(),
        "system_mode": str(cfg["system_mode"]).strip().lower(),
        "system_file": abspath(cfg["system_file"]),
        "system_append": str(cfg["system_append"]),
        "log_path": abspath(cfg["log_path"]),
        "timeout": float(cfg["timeout"]),
    })


def fmt_tokens(n):
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def tail_text(path, max_lines=300, max_bytes=131072):
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    return "\n".join(data.splitlines()[-max_lines:])


def read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_json_file(path, obj):
    """原子写。临时名带 pid，主路径被占用 / 只读时再直写兜底。"""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    data = json.dumps(obj, ensure_ascii=False, indent=2)
    last = None
    for tmp in (path + ".pj.tmp", path + ".pj.%d.tmp" % os.getpid()):
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, path)
            return
        except OSError as exc:
            last = exc
            try:
                if os.path.isfile(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data)
    except OSError:
        raise last


def dir_writable(path):
    """能否在该目录里建文件：返回 (bool, 原因)。"""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        return False, str(exc)
    probe = os.path.join(path, ".pj_write_probe_%d" % os.getpid())
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("x")
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            if os.path.isfile(probe):
                os.remove(probe)
        except OSError:
            pass
    return True, ""


def port_open(host, port, timeout=0.3):
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except OSError:
        return False


def url_hostport(url):
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return host, port
    except ValueError:
        return "", 0


# ------------------------------------------------------------------ 主界面

class StudioGUI:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.server = None
        self.server_thread = None
        self._last_log = None
        self._busy = False
        self._tick = 0
        self._guard_hits = 0

        root.title(APP_NAME)
        root.geometry("980x700")
        root.minsize(860, 580)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._make_vars()
        self._build_ui()
        self._load_into_widgets()
        self.refresh_cc()
        self._poll()

    # ---------------------------------------------------------- 变量

    def _make_vars(self):
        d = self.cfg
        self.v_port = tk.StringVar(value=str(d["port"]))
        self.v_upstream = tk.StringVar(value=d["upstream"])
        self.v_apikey = tk.StringVar(value=d["api_key"])
        self.v_model = tk.StringVar(value=d["model"])
        self.v_mode = tk.StringVar(value=d["system_mode"])
        self.v_sysfile = tk.StringVar(value=d["system_file"])
        self.v_append = tk.StringVar(value=d["system_append"])
        self.v_logfile = tk.StringVar(value=d["log_path"])
        self.v_timeout = tk.StringVar(value=str(d["timeout"]))
        self.v_status = tk.StringVar(value="未运行")
        self.v_stats = tk.StringVar(value="")
        self.v_autoscroll = tk.BooleanVar(value=True)
        self.v_showkey = tk.BooleanVar(value=False)
        self.v_guard = tk.BooleanVar(value=bool(d["takeover"].get("guard", True)))

        self.v_cc_path = tk.StringVar(value=CLAUDE_SETTINGS)
        self.v_cc_base = tk.StringVar(value="")
        self.v_cc_token = tk.StringVar(value="")
        self.v_cc_switch = tk.StringVar(value="")
        self.v_cc_gw = tk.StringVar(value="")
        self.v_cc_take = tk.StringVar(value="")
        self.v_cc_perm = tk.StringVar(value="")
        self.v_syscost = tk.StringVar(value="")
        self.v_usage = tk.StringVar(value="本次：—")

    # ---------------------------------------------------------- 布局

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self.nb = ttk.Notebook(outer)
        self.nb.pack(fill="both", expand=True)

        self.tab_gw = ttk.Frame(self.nb, padding=12)
        self.tab_cc = ttk.Frame(self.nb, padding=12)
        self.tab_sys = ttk.Frame(self.nb, padding=12)
        self.tab_log = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.tab_gw, text="  网关配置  ")
        self.nb.add(self.tab_cc, text="  Claude Code 对接  ")
        self.nb.add(self.tab_sys, text="  系统提示词  ")
        self.nb.add(self.tab_log, text="  运行日志  ")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh_cc())

        self._build_gateway_tab()
        self._build_cc_tab()
        self._build_system_tab()
        self._build_log_tab()

        for var in (self.v_mode, self.v_append, self.v_sysfile, self.v_logfile):
            var.trace_add("write", lambda *a: self.update_cost())

        bar = ttk.Frame(outer, padding=(0, 8, 0, 0))
        bar.pack(fill="x")
        self.lamp = tk.Canvas(bar, width=14, height=14, highlightthickness=0)
        self.lamp.pack(side="left")
        self._lamp_id = self.lamp.create_oval(2, 2, 12, 12, fill="#999999", outline="")
        ttk.Label(bar, textvariable=self.v_status, width=44).pack(side="left", padx=8)
        ttk.Label(bar, textvariable=self.v_stats).pack(side="left")
        ttk.Label(bar, textvariable=self.v_usage, foreground="#2f5d8a").pack(side="left", padx=12)
        ttk.Label(bar, text=f"配置目录：{APP_DIR}", foreground="#666666").pack(side="right")

    def _build_gateway_tab(self):
        frm = ttk.LabelFrame(self.tab_gw, text=" 转发参数 ", padding=12)
        frm.pack(fill="x")
        frm.columnconfigure(1, weight=1)

        def row(idx, label, widget):
            ttk.Label(frm, text=label).grid(row=idx, column=0, sticky="w", pady=5, padx=(0, 10))
            widget.grid(row=idx, column=1, sticky="ew", pady=5)

        row(0, "监听端口", ttk.Entry(frm, textvariable=self.v_port, width=10))
        row(1, "上游地址", ttk.Entry(frm, textvariable=self.v_upstream))

        keybox = ttk.Frame(frm)
        self.e_apikey = ttk.Entry(keybox, textvariable=self.v_apikey, show="*")
        self.e_apikey.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(keybox, text="显示", variable=self.v_showkey,
                        command=self._toggle_key).pack(side="left", padx=(8, 0))
        row(2, "API Key（可空）", keybox)

        row(3, "默认模型", ttk.Entry(frm, textvariable=self.v_model, width=32))

        modebox = ttk.Frame(frm)
        self.cb_mode = ttk.Combobox(modebox, textvariable=self.v_mode, values=SYSTEM_MODES,
                                    state="readonly", width=14)
        self.cb_mode.pack(side="left")
        self.cb_mode.bind("<<ComboboxSelected>>", lambda e: self.update_cost())
        ttk.Label(modebox, text="passthrough 透传 / append 追加 / replace 替换",
                  foreground="#666666").pack(side="left", padx=10)
        row(4, "system 策略", modebox)

        filebox = ttk.Frame(frm)
        ttk.Entry(filebox, textvariable=self.v_sysfile).pack(side="left", fill="x", expand=True)
        ttk.Button(filebox, text="浏览…", width=7,
                   command=self._pick_sysfile).pack(side="left", padx=(8, 0))
        row(5, "system 文件", filebox)

        row(6, "追加内容", ttk.Entry(frm, textvariable=self.v_append))

        logbox = ttk.Frame(frm)
        ttk.Entry(logbox, textvariable=self.v_logfile).pack(side="left", fill="x", expand=True)
        ttk.Button(logbox, text="浏览…", width=7,
                   command=self._pick_logfile).pack(side="left", padx=(8, 0))
        row(7, "日志文件", logbox)

        row(8, "上游超时(秒)", ttk.Entry(frm, textvariable=self.v_timeout, width=10))

        act = ttk.LabelFrame(self.tab_gw, text=" 控制 ", padding=12)
        act.pack(fill="x", pady=(12, 0))
        ttk.Button(act, text="保存配置", command=self.save).pack(side="left")
        self.btn_start = ttk.Button(act, text="启动网关", command=self.start)
        self.btn_start.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(act, text="停止网关", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(act, text="测试请求", command=self.test).pack(side="left", padx=6)

        hint = ("串联模式下上游填 cc-switch 的本地代理地址（如 http://127.0.0.1:15721），"
                "API Key 留空，鉴权头会原样透传给 cc-switch。\n"
                "端口与上游在启动时生效；system 文件与追加内容每次请求实时读取。")
        ttk.Label(self.tab_gw, text=hint, foreground="#666666", wraplength=900,
                  justify="left").pack(fill="x", pady=(10, 0))

        self.lbl_cost = ttk.Label(self.tab_gw, textvariable=self.v_syscost,
                                  foreground="#2e8b57", wraplength=900, justify="left")
        self.lbl_cost.pack(fill="x", pady=(6, 0))

    def _build_cc_tab(self):
        info = ttk.LabelFrame(self.tab_cc, text=" 当前状态 ", padding=12)
        info.pack(fill="x")
        info.columnconfigure(1, weight=1)
        rows = [
            ("Claude Code 配置", self.v_cc_path),
            ("当前 BASE_URL", self.v_cc_base),
            ("当前 TOKEN", self.v_cc_token),
            ("cc-switch", self.v_cc_switch),
            ("本网关", self.v_cc_gw),
            ("接管状态", self.v_cc_take),
            ("配置目录权限", self.v_cc_perm),
        ]
        for idx, (label, var) in enumerate(rows):
            ttk.Label(info, text=label).grid(row=idx, column=0, sticky="w",
                                             pady=4, padx=(0, 14))
            ttk.Label(info, textvariable=var, wraplength=700,
                      justify="left").grid(row=idx, column=1, sticky="w", pady=4)

        act = ttk.LabelFrame(self.tab_cc, text=" 操作 ", padding=12)
        act.pack(fill="x", pady=(12, 0))
        ttk.Button(act, text="刷新检测", command=self.refresh_cc).pack(side="left")
        ttk.Button(act, text="上游改用 cc-switch 代理",
                   command=self.use_ccswitch_upstream).pack(side="left", padx=6)
        ttk.Button(act, text="接管到本网关", command=self.take_over).pack(side="left")
        ttk.Button(act, text="还原配置", command=self.restore_config).pack(side="left", padx=6)
        ttk.Button(act, text="打开配置目录",
                   command=lambda: self.open_path(CLAUDE_DIR)).pack(side="left")
        ttk.Button(act, text="以管理员身份重启",
                   command=self.restart_elevated).pack(side="left", padx=6)

        opt = ttk.Frame(self.tab_cc)
        opt.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(opt, text="cc-switch 覆盖 settings.json 后自动重接管",
                        variable=self.v_guard,
                        command=self.save_guard).pack(side="left")

        ttk.Button(self.tab_cc, text="复制 CLI 环境变量（命令行用）",
                   command=self.copy_env).pack(anchor="w", pady=(10, 0))

        hint = (
            "工作方式：本网关插在 Claude Code 与 cc-switch 之间，"
            "只改 settings.json 的 ANTHROPIC_BASE_URL，其余字段（token、模型映射）原样保留。\n"
            "① 网关配置页把上游设为 cc-switch 代理地址，保存并启动网关；"
            "② 这里点『接管到本网关』；③ VS Code 里重开一个 Claude Code 会话。\n"
            "还原时只把 BASE_URL 写回原值，不会覆盖 cc-switch 写的其它内容。"
        )
        ttk.Label(self.tab_cc, text=hint, foreground="#666666", wraplength=900,
                  justify="left").pack(fill="x", pady=(12, 0))

    def _build_system_tab(self):
        bar = ttk.Frame(self.tab_sys)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="从文件读取", command=self.sys_reload).pack(side="left")
        ttk.Button(bar, text="保存到文件", command=self.sys_save).pack(side="left", padx=6)
        ttk.Button(bar, text="载入默认人格", command=self.sys_load_default).pack(side="left")
        ttk.Button(bar, text="恢复出厂", command=self.sys_factory_reset).pack(side="left", padx=6)
        ttk.Button(bar, text="导入…", command=self.sys_import).pack(side="left")
        ttk.Button(bar, text="导出…", command=self.sys_export).pack(side="left", padx=6)
        ttk.Button(bar, text="清空", command=self.sys_clear).pack(side="left")

        bar2 = ttk.Frame(self.tab_sys)
        bar2.pack(fill="x", pady=(0, 8))
        self.v_sysmeta = tk.StringVar(value="")
        ttk.Label(bar2, textvariable=self.v_sysmeta, foreground="#666666").pack(side="left")
        self.v_sysdefault = tk.StringVar(
            value="出厂人格：破甲一键通 v8.42 核心（阿岩 · 全解锁人格 + 政策 + 硬化 + 强化段）"
                  f" · {len(pj_persona.DEFAULT_PERSONA)} 字符")
        ttk.Label(bar2, textvariable=self.v_sysdefault,
                  foreground="#888888").pack(side="right")

        self.txt_system = scrolledtext.ScrolledText(
            self.tab_sys, wrap="word", undo=True,
            font=(UIFONT, 10), relief="solid", borderwidth=1)
        self.txt_system.pack(fill="both", expand=True)
        self.txt_system.bind("<KeyRelease>", lambda e: self._sys_dirty())

        ttk.Label(self.tab_sys,
                  text="replace 模式整段替换客户端系统提示；append 模式下「追加内容」为空时用全文、"
                       "非空时只追加该字段。改完点『保存到文件』，下一次请求即生效，无需重启网关。\n"
                       "出厂人格已内置在程序里：『恢复出厂』可随时回到原始稿，『导出…』可另存一份自己维护。",
                  foreground="#666666", wraplength=900, justify="left").pack(fill="x", pady=(8, 0))

    def _build_log_tab(self):
        bar = ttk.Frame(self.tab_log)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Button(bar, text="立即刷新", command=self.log_refresh).pack(side="left")
        ttk.Button(bar, text="清空显示", command=self.log_clear_view).pack(side="left", padx=6)
        ttk.Button(bar, text="清空日志文件", command=self.log_wipe).pack(side="left")
        ttk.Button(bar, text="打开目录",
                   command=lambda: self.open_path(os.path.dirname(abspath(self.v_logfile.get())) or APP_DIR)
                   ).pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="自动滚动", variable=self.v_autoscroll).pack(side="right")

        self.txt_log = scrolledtext.ScrolledText(
            self.tab_log, wrap="none", state="disabled",
            font=(UIFONT, 9), relief="solid", borderwidth=1)
        self.txt_log.pack(fill="both", expand=True)

    # ---------------------------------------------------------- 控件行为

    def _toggle_key(self):
        self.e_apikey.configure(show="" if self.v_showkey.get() else "*")

    def _pick_sysfile(self):
        path = filedialog.asksaveasfilename(
            title="选择或用新文件名创建 system 提示词文件",
            initialdir=APP_DIR,
            initialfile=os.path.basename(self.v_sysfile.get()) or "system_override.txt",
            defaultextension=".txt", filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")])
        if path:
            self.v_sysfile.set(path)

    def _pick_logfile(self):
        path = filedialog.asksaveasfilename(
            title="选择日志文件", initialdir=APP_DIR,
            initialfile=os.path.basename(self.v_logfile.get()) or "gateway.jsonl",
            defaultextension=".jsonl", filetypes=[("JSONL", "*.jsonl"), ("全部文件", "*.*")])
        if path:
            self.v_logfile.set(path)

    def open_path(self, path):
        try:
            if IS_MAC:
                target = path if os.path.isdir(path) else os.path.dirname(path) or APP_DIR
                subprocess.Popen(["open", target])
            elif IS_WIN:
                if os.path.isfile(path):
                    subprocess.Popen(["explorer", "/select,", path])
                else:
                    os.startfile(path)  # noqa: S606
            else:
                target = path if os.path.isdir(path) else os.path.dirname(path) or APP_DIR
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"打开失败：{exc}")

    # ---------------------------------------------------------- 配置装载

    def _load_into_widgets(self):
        self.v_port.set(str(self.cfg["port"]))
        self.v_upstream.set(self.cfg["upstream"])
        self.v_apikey.set(self.cfg["api_key"])
        self.v_model.set(self.cfg["model"])
        self.v_mode.set(self.cfg["system_mode"] if self.cfg["system_mode"] in SYSTEM_MODES
                        else "passthrough")
        self.v_sysfile.set(self.cfg["system_file"])
        self.v_append.set(self.cfg["system_append"])
        self.v_logfile.set(self.cfg["log_path"])
        self.v_timeout.set(str(self.cfg["timeout"]))
        self.sys_ensure_default()
        self.sys_reload()
        self.update_cost()

    def collect(self):
        try:
            port = int(str(self.v_port.get()).strip())
        except ValueError:
            raise ValueError("监听端口必须是整数")
        if not (1 <= port <= 65535):
            raise ValueError("监听端口必须在 1-65535 之间")

        upstream = str(self.v_upstream.get()).strip()
        if not upstream.startswith(("http://", "https://")):
            raise ValueError("上游地址必须以 http:// 或 https:// 开头")

        try:
            timeout = float(str(self.v_timeout.get()).strip())
        except ValueError:
            raise ValueError("上游超时必须是数字")
        if timeout < 5:
            raise ValueError("上游超时至少 5 秒")

        return {
            "port": port,
            "upstream": upstream,
            "api_key": str(self.v_apikey.get()).strip(),
            "model": str(self.v_model.get()).strip() or "deepseek-flash",
            "system_mode": str(self.v_mode.get()).strip().lower() or "replace",
            "system_file": str(self.v_sysfile.get()).strip() or "system_override.txt",
            "system_append": str(self.v_append.get()),
            "log_path": str(self.v_logfile.get()).strip() or "gateway.jsonl",
            "timeout": timeout,
            "takeover": dict(self.cfg.get("takeover") or {}),
        }

    def save_guard(self):
        self.cfg.setdefault("takeover", {})["guard"] = bool(self.v_guard.get())
        try:
            save_config(self.cfg)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存失败：{exc}")

    def save(self):
        try:
            self.cfg = self.collect()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return False
        try:
            save_config(self.cfg)
            apply_to_engine(self.cfg)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存配置失败：{exc}")
            return False
        self.v_status.set("配置已保存")
        self.refresh_cc()
        return True

    # ---------------------------------------------------------- 网关启停

    def start(self):
        if self.server is not None:
            messagebox.showinfo(APP_NAME, "网关已在运行")
            return
        if not self.save():
            return

        u_host, u_port = url_hostport(str(self.cfg["upstream"]))
        if u_host in ("127.0.0.1", "localhost", "::1") and u_port == int(self.cfg["port"]):
            messagebox.showerror(
                APP_NAME,
                f"上游地址填的是本网关自己的端口（{self.cfg['upstream']}）。\n\n"
                "这样请求会自己转发给自己，转几圈后端口耗尽，表现为「测试请求失败 / 卡住几十秒」。\n\n"
                f"应改成 cc-switch 的代理地址，通常是 http://127.0.0.1:15721。\n"
                "可以在「Claude Code 对接」页点『上游改用 cc-switch 代理』自动填。")
            return

        if self.cfg["system_mode"] == "replace":
            text = self.txt_system.get("1.0", "end").strip()
            if not text:
                if not messagebox.askyesno(APP_NAME, "当前是 replace 模式但 system 文件为空，"
                                                     "所有请求将不带系统提示。继续启动？"):
                    return
        try:
            srv = pj_gateway.ThreadingHTTPServer(("127.0.0.1", int(self.cfg["port"])),
                                                 pj_gateway.GatewayHandler)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"端口 {self.cfg['port']} 绑定失败：{exc}")
            return
        srv.daemon_threads = True
        pj_gateway.reset_usage()
        self.v_usage.set("本次：—")
        self.server = srv
        self.server_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        self.server_thread.start()
        self.v_status.set(f"运行中 · http://127.0.0.1:{self.cfg['port']}")
        self._set_lamp("#2e8b57")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._sync_takeover_url()
        self.refresh_cc()

    def stop(self):
        if self.server is None:
            return
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 停止异常: {exc}", file=sys.stderr)
        self.server = None
        self.server_thread = None
        self.v_status.set("已停止")
        self.v_stats.set("")
        self._set_lamp("#999999")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.refresh_cc()

    def _gateway_url(self, port=None):
        return f"http://127.0.0.1:{port or self.cfg['port']}"

    def _fmt_usage(self, usage):
        if not isinstance(usage, dict):
            return "本次：—"
        inp = int(usage.get("input") or 0)
        out = int(usage.get("output") or 0)
        cache = int(usage.get("cache_read") or 0)
        calls = int(usage.get("calls") or 0)
        if not (inp or out or calls):
            return "本次：—"
        text = f"本次 {calls} 次 · 输入 {fmt_tokens(inp)} · 输出 {fmt_tokens(out)}"
        if cache:
            text += f" · 缓存读 {fmt_tokens(cache)}"
        return text

    # ---------------------------------------------------------- 系统提示体积预估

    def _injected_chars(self):
        mode = str(self.v_mode.get() or "replace").lower()
        text = ""
        path = self._sys_path()
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = ""
        if mode == "replace":
            return len(text), "replace"
        if mode == "append":
            extra = self.v_append.get()
            return (len(extra) if extra.strip() else len(text)), "append"
        return 0, "passthrough"

    def _client_base_chars(self):
        """最近一次请求里客户端自己发的系统提示长度（日志里的 system_in_chars）。"""
        path = abspath(self.v_logfile.get().strip() or "gateway.jsonl")
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 65536))
                chunk = fh.read().decode("utf-8", "replace")
        except OSError:
            return 12586, False
        for raw in reversed(chunk.splitlines()):
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            val = obj.get("system_in_chars")
            if isinstance(val, int) and val > 0:
                return val, True
        return 12586, False

    def update_cost(self):
        if not hasattr(self, "lbl_cost"):
            return
        injected, mode = self._injected_chars()
        base, measured = self._client_base_chars()
        tail = "" if measured else "　（客户端基线用默认 12,586，跑过一次后自动校准）"
        if mode == "replace":
            total = injected
            msg = (f"预计系统提示：{total:,} 字符 ≈ {total / 1.74:,.0f} tokens"
                   f"　｜ replace：只发注入内容，客户端提示被整段替换")
            color = "#2e8b57"
        elif mode == "append":
            total = injected + base
            msg = (f"预计系统提示：{total:,} 字符 ≈ {total / 1.74:,.0f} tokens"
                   f"　｜ append：客户端 {base:,} + 注入 {injected:,}"
                   f"（比 replace 多发 {base:,} 字符）")
            color = "#b45309"
        else:
            total = base
            msg = (f"预计系统提示：{total:,} 字符 ≈ {total / 1.74:,.0f} tokens"
                   f"　｜ passthrough：原样转发，人格不会生效")
            color = "#8a6d3b"
        self.v_syscost.set(msg + tail)
        self.lbl_cost.configure(foreground=color)

    def _sync_takeover_url(self):
        take = self.cfg.setdefault("takeover", {})
        if not take.get("active"):
            return
        gw = self._gateway_url()
        if take.get("gateway_url") != gw:
            take["gateway_url"] = gw
            save_config(self.cfg)
            self._apply_takeover(gw)

    # ---------------------------------------------------------- cc-switch 检测

    def detect_ccswitch(self):
        info = {
            "installed": os.path.isdir(CCSWITCH_DIR),
            "proxy_mode": False,
            "state_path": CCSWITCH_STATE if os.path.isfile(CCSWITCH_STATE) else "",
        }
        state = read_json_file(CCSWITCH_STATE) if info["state_path"] else None
        if isinstance(state, dict):
            app = (state.get("apps") or {}).get("claude") or {}
            info["proxy_mode"] = app.get("mode") == "proxy"
            info["route"] = app.get("proxy_route") or ""

        settings = read_json_file(CLAUDE_SETTINGS) or {}
        env = settings.get("env") or {}
        base = str(env.get("ANTHROPIC_BASE_URL") or "")
        info["base_url"] = base
        info["token"] = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "")

        host, port = url_hostport(base)
        info["proxy_url"] = ""
        if host in ("127.0.0.1", "localhost") and port:
            info["proxy_url"] = f"http://{host}:{port}"
            info["proxy_alive"] = port_open(host, port)
        else:
            info["proxy_alive"] = False
        return info

    def refresh_cc(self):
        try:
            info = self.detect_ccswitch()
        except Exception as exc:  # noqa: BLE001
            self.v_cc_base.set(f"读取失败：{exc}")
            return

        self.v_cc_path.set(CLAUDE_SETTINGS if os.path.isfile(CLAUDE_SETTINGS)
                           else f"{CLAUDE_SETTINGS}（不存在）")
        self.v_cc_base.set(info["base_url"] or "（未设置）")
        token = info["token"]
        if token and token != "PROXY_MANAGED":
            token = token[:6] + "…" + token[-4:] if len(token) > 12 else "***"
        self.v_cc_token.set(token or "（未设置）")

        if info["proxy_url"]:
            alive = "运行中" if info["proxy_alive"] else "端口无响应"
            mode = "代理模式" if info["proxy_mode"] else "直连/未知"
            self.v_cc_switch.set(f"{info['proxy_url']} · {mode} · {alive}")
        elif info["installed"]:
            self.v_cc_switch.set("已安装，但当前不是本地代理模式")
        else:
            self.v_cc_switch.set("未检测到")

        gw = self._gateway_url()
        self.v_cc_gw.set(f"{gw}（{'运行中' if self.server else '未启动'}）")

        take = self.cfg.get("takeover") or {}
        if take.get("active"):
            text = f"已接管 → BASE_URL 指向 {take.get('gateway_url') or gw}"
            if self._guard_hits:
                text += f"；已自动重接管 {self._guard_hits} 次"
            self.v_cc_take.set(text)
        else:
            self.v_cc_take.set("未接管（Claude Code 直连 cc-switch）")

        ok, why = dir_writable(CLAUDE_DIR)
        self.v_cc_perm.set("可写" if ok else f"不可写 —— {why}")

    def use_ccswitch_upstream(self):
        info = self.detect_ccswitch()
        url = info.get("proxy_url")
        if not url:
            messagebox.showinfo(APP_NAME,
                                "没有检测到 cc-switch 的本地代理地址。\n"
                                "请先在 cc-switch 里对 Claude 开启本地代理，再回来刷新。")
            return
        self.v_upstream.set(url)
        self.v_apikey.set("")
        if messagebox.askyesno(APP_NAME,
                               f"上游已改为 {url}，API Key 已清空（鉴权由 cc-switch 接管）。\n"
                               "是否顺便保存并重启网关使其生效？"):
            if self.server is not None:
                self.stop()
            self.start()

    # ---------------------------------------------------------- 接管 / 还原

    def take_over(self):
        try:
            cfg = self.collect()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        if self.server is None:
            if not messagebox.askyesno(APP_NAME,
                                       "网关还没启动。接管后 Claude Code 会指向一个没在监听的本机端口，"
                                       "请求会全部失败。\n仍要继续接管吗？"):
                return

        gw = self._gateway_url(cfg["port"])

        # 先验权限：目录不可写时，后面每一步都注定失败，直接给可执行的处置方法
        ok, why = dir_writable(CLAUDE_DIR)
        if not ok:
            self._permission_help(why)
            return

        settings = read_json_file(CLAUDE_SETTINGS)
        if settings is None:
            settings = {}
        env = settings.get("env")
        if not isinstance(env, dict):
            env = {}
            settings["env"] = env

        current = str(env.get("ANTHROPIC_BASE_URL") or "")
        take = self.cfg.setdefault("takeover", {})

        if not take.get("active"):
            backup = CLAUDE_BACKUP
            try:
                write_json_file(backup, settings)
            except OSError:
                # ~/.claude 写不进备份时，退到程序自己的目录，不阻断接管
                backup = os.path.join(APP_DIR, "settings.pj-backup.json")
                try:
                    write_json_file(backup, settings)
                except OSError as exc:
                    messagebox.showerror(APP_NAME, f"备份 settings.json 失败：{exc}")
                    return
                self.v_status.set(f"备份已改存到 {os.path.basename(backup)}")
            take["original"] = current
            take["backup"] = backup

        env["ANTHROPIC_BASE_URL"] = gw
        try:
            write_json_file(CLAUDE_SETTINGS, settings)
        except OSError as exc:
            self._permission_help(str(exc))
            return

        take["active"] = True
        take["gateway_url"] = gw
        take["guard"] = bool(self.v_guard.get())
        save_config(self.cfg)

        self.refresh_cc()
        self.v_status.set("已接管，去 VS Code 重开 Claude Code 会话")
        messagebox.showinfo(APP_NAME,
                            f"已接管。\n\nBASE_URL：{gw}\n\n"
                            "现在去 VS Code 里结束旧的 Claude Code 会话再新开一个，"
                            "新会话才会读取新的 BASE_URL。")

    def restore_config(self):
        take = self.cfg.setdefault("takeover", {})
        settings = read_json_file(CLAUDE_SETTINGS)
        if settings is None:
            settings = {}
        env = settings.get("env")
        if not isinstance(env, dict):
            env = {}
            settings["env"] = env

        original = str(take.get("original") or "")
        if original:
            env["ANTHROPIC_BASE_URL"] = original
        else:
            env.pop("ANTHROPIC_BASE_URL", None)

        try:
            write_json_file(CLAUDE_SETTINGS, settings)
        except OSError as exc:
            self._permission_help(str(exc))
            return

        take["active"] = False
        save_config(self.cfg)
        self.refresh_cc()
        self.v_status.set("已还原 Claude Code 配置")

    def _permission_help(self, why):
        gw = self._gateway_url()
        if IS_MAC:
            messagebox.showerror(
                APP_NAME,
                f"无法写入 {CLAUDE_DIR}\n\n系统返回：{why}\n\n"
                "macOS 上常见原因：\n"
                "1) 该文件被系统或安全软件锁定\n"
                "2) 目录属主不是当前用户（chown 错）\n"
                "3) 通过 App Store 沙盒版安装，容器不允许写\n\n"
                "按顺序试：\n"
                "① 在终端执行：ls -l ~/.claude/settings.json  看属主与权限\n"
                "② chmod u+w ~/.claude/settings.json\n"
                f"③ 手动改：用编辑器打开\n  {CLAUDE_SETTINGS}\n"
                f"  把 env.ANTHROPIC_BASE_URL 改成 {gw}，保存即可")
            return

        messagebox.showerror(
            APP_NAME,
            f"无法写入 {CLAUDE_DIR}\n\n系统返回：{why}\n\n"
            "最常见的原因是「完整性级别」：本程序所在目录如果带 "
            "Low Mandatory Level 标记（agent 工作区、沙箱目录经常如此），"
            "exe 会继承低完整性，对 .claude 这类中等完整性对象一律写不进去 —— "
            "此时即使是管理员身份也没用。\n\n"
            "其它可能：杀软拦截、目录只读、文件被独占。\n\n"
            "先按这个顺序试：\n"
            "① 把 exe 复制到桌面或任意普通目录再运行\n"
            "② 或在管理员 PowerShell 里执行：\n"
            f'   icacls "{sys.executable}" /setintegritylevel Medium\n'
            "③ 点『以管理员身份重启』\n"
            "④ 手动改：记事本打开\n"
            f"  {CLAUDE_SETTINGS}\n"
            f"  把 env.ANTHROPIC_BASE_URL 改成 {gw}，保存即可")

    def restart_elevated(self):
        try:
            exe = sys.executable
            if IS_MAC:
                script = 'do shell script "%s" with administrator privileges' % exe.replace('"', '\\"')
                subprocess.Popen(["osascript", "-e", script])
            else:
                import ctypes
                rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, APP_DIR, 1)
                if rc <= 32:
                    raise OSError(f"ShellExecuteW 返回 {rc}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"提权重启失败：{exc}")
            return
        if self.server is not None:
            try:
                self.stop()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    def _apply_takeover(self, gw):
        settings = read_json_file(CLAUDE_SETTINGS)
        if settings is None:
            return False
        env = settings.get("env")
        if not isinstance(env, dict):
            env = {}
            settings["env"] = env
        if env.get("ANTHROPIC_BASE_URL") == gw:
            return False
        env["ANTHROPIC_BASE_URL"] = gw
        try:
            write_json_file(CLAUDE_SETTINGS, settings)
            return True
        except OSError:
            return False

    def guard_takeover(self):
        take = self.cfg.get("takeover") or {}
        if not take.get("active") or not self.v_guard.get():
            return
        gw = take.get("gateway_url") or self._gateway_url()
        settings = read_json_file(CLAUDE_SETTINGS)
        if settings is None:
            return
        env = settings.get("env") or {}
        if str(env.get("ANTHROPIC_BASE_URL") or "") == gw:
            return
        if self._apply_takeover(gw):
            self._guard_hits += 1
            self.v_status.set(f"cc-switch 覆盖了配置，已重接管（第 {self._guard_hits} 次）")
            self.refresh_cc()

    # ---------------------------------------------------------- 测试 / 环境

    def test(self):
        if self.server is None:
            messagebox.showinfo(APP_NAME, "请先启动网关")
            return
        if self._busy:
            return
        self._busy = True
        self.v_status.set("测试中…")

        token = str(self.v_apikey.get()).strip() or "PROXY_MANAGED"

        def worker():
            payload = json.dumps({
                "model": self.cfg["model"],
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "只回复两个字：通了"}],
            }).encode("utf-8")
            req = urlrequest.Request(
                f"http://127.0.0.1:{self.cfg['port']}/v1/messages",
                data=payload, method="POST",
                headers={
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "authorization": f"Bearer {token}",
                    "x-api-key": token,
                })
            started = time.time()
            try:
                with urlrequest.urlopen(req, timeout=120) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    code = resp.status
            except urlerror.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                code = exc.code
            except Exception as exc:  # noqa: BLE001
                body = f"{type(exc).__name__}: {exc}"
                code = 0
            elapsed = int((time.time() - started) * 1000)
            self.root.after(0, lambda: self._finish_test(code, body, elapsed))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_test(self, code, body, elapsed):
        self._busy = False
        self.v_status.set(f"运行中 · {self._gateway_url()}")
        if code == 200:
            text = ""
            try:
                obj = json.loads(body)
                for block in obj.get("content", []):
                    if block.get("type") == "text":
                        text += block.get("text", "")
                usage = obj.get("usage", {})
                messagebox.showinfo(
                    APP_NAME,
                    f"状态码 200 · {elapsed} ms\n"
                    f"输出：{text[:200] or '（空，可能是思考型模型用完了 max_tokens）'}\n"
                    f"tokens：in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
            except json.JSONDecodeError:
                messagebox.showinfo(APP_NAME, f"状态码 200 · {elapsed} ms\n{body[:400]}")
        else:
            messagebox.showerror(APP_NAME, f"状态码 {code} · {elapsed} ms\n\n{body[:800]}")

    def copy_env(self):
        try:
            cfg = self.collect()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        text = "\n".join([
            f'$env:ANTHROPIC_BASE_URL="{self._gateway_url(cfg["port"])}"',
            '$env:ANTHROPIC_AUTH_TOKEN="PROXY_MANAGED"',
        ])
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.v_status.set("环境变量已复制到剪贴板")

    # ---------------------------------------------------------- 系统提示词

    def _sys_path(self):
        return abspath(self.v_sysfile.get().strip() or "system_override.txt")

    def sys_ensure_default(self):
        """首次运行：system 文件不存在时，用内置出厂人格初始化一份可改的副本。"""
        path = self._sys_path()
        if not path or os.path.isfile(path):
            return False
        try:
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(pj_persona.DEFAULT_PERSONA)
            return True
        except OSError as exc:
            print(f"[warn] 初始化人格文件失败: {exc}", file=sys.stderr)
            return False

    def sys_reload(self):
        path = self._sys_path()
        text = ""
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"读取失败：{exc}")
                return
        else:
            text = pj_persona.DEFAULT_PERSONA
        self.txt_system.delete("1.0", "end")
        self.txt_system.insert("1.0", text)
        self._sys_dirty()

    def sys_load_default(self):
        """把内置出厂人格灌进编辑框（不落盘，用户可先改再保存）。"""
        self.txt_system.delete("1.0", "end")
        self.txt_system.insert("1.0", pj_persona.DEFAULT_PERSONA)
        self._sys_dirty()
        self.v_status.set("已载入出厂人格，点『保存到文件』生效")

    def sys_factory_reset(self):
        if not messagebox.askyesno(APP_NAME,
                                   "恢复出厂会把当前人格文件整段覆盖为内置默认稿"
                                   "（破甲一键通 v8.42 核心）。继续？"):
            return
        self.txt_system.delete("1.0", "end")
        self.txt_system.insert("1.0", pj_persona.DEFAULT_PERSONA)
        self.sys_save()

    def sys_import(self):
        path = filedialog.askopenfilename(
            title="导入人格文件",
            initialdir=APP_DIR,
            filetypes=[("Markdown / 文本", "*.md *.txt"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"读取失败：{exc}")
            return
        except UnicodeDecodeError:
            messagebox.showerror(APP_NAME, "不是 UTF-8 文本，请另存为 UTF-8 再导入")
            return
        self.txt_system.delete("1.0", "end")
        self.txt_system.insert("1.0", text)
        self._sys_dirty()
        self.v_status.set(f"已导入 {os.path.basename(path)}，点『保存到文件』生效")

    def sys_export(self):
        path = filedialog.asksaveasfilename(
            title="导出现有人格",
            initialdir=APP_DIR,
            initialfile="persona-custom.md",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("文本文件", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.txt_system.get("1.0", "end-1c"))
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}")
            return
        self.v_status.set(f"已导出到 {os.path.basename(path)}")

    def sys_save(self):
        path = self._sys_path()
        text = self.txt_system.get("1.0", "end-1c")
        try:
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存失败：{exc}")
            return
        self._sys_dirty()
        self.update_cost()
        self.v_status.set(f"系统提示词已写入 {os.path.basename(path)}（{len(text)} 字符）")

    def sys_clear(self):
        self.txt_system.delete("1.0", "end")
        self._sys_dirty()

    def _sys_dirty(self):
        path = self._sys_path()
        text = self.txt_system.get("1.0", "end-1c")
        size = len(text)
        default = pj_persona.DEFAULT_PERSONA
        if text.strip() == default.strip():
            tag = "出厂人格"
        else:
            delta = size - len(default)
            tag = f"已自定义（较出厂 {delta:+d} 字符）" if delta else "已自定义"
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    if fh.read() != text:
                        tag += " · 未保存"
            except OSError:
                tag += " · 读取异常"
        else:
            tag += " · 文件未创建"
        self.v_sysmeta.set(f"{os.path.basename(path)} · {size} 字符 · {tag}")

    # ---------------------------------------------------------- 日志

    def log_refresh(self):
        path = abspath(self.v_logfile.get().strip() or "gateway.jsonl")
        text = tail_text(path)
        if text == self._last_log:
            return
        self._last_log = text
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.insert("1.0", text or "（暂无日志）")
        self.txt_log.configure(state="disabled")
        if self.v_autoscroll.get():
            self.txt_log.see("end")

    def log_clear_view(self):
        self._last_log = None
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def log_wipe(self):
        path = abspath(self.v_logfile.get().strip() or "gateway.jsonl")
        if not os.path.isfile(path):
            return
        if not messagebox.askyesno(APP_NAME, f"删除日志文件？\n{path}"):
            return
        try:
            os.remove(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"删除失败：{exc}")
            return
        self.log_clear_view()

    # ---------------------------------------------------------- 轮询

    def _set_lamp(self, color):
        self.lamp.itemconfigure(self._lamp_id, fill=color)

    def _poll(self):
        try:
            self._tick += 1
            if self.server is not None:
                url = f"http://127.0.0.1:{self.cfg['port']}/__health"
                try:
                    with urlrequest.urlopen(url, timeout=0.6) as resp:
                        health = json.loads(resp.read().decode("utf-8"))
                    stats = health.get("stats", {})
                    self.v_stats.set(
                        f"请求 {stats.get('requests', 0)} · 错误 {stats.get('errors', 0)} · "
                        f"运行 {stats.get('uptime_s', 0)}s")
                    self.v_usage.set(self._fmt_usage(health.get("usage") or {}))
                except Exception:  # noqa: BLE001
                    self.v_stats.set("健康检查无响应")

            if self._tick % 2 == 0:
                self.guard_takeover()
                self.refresh_cc()
                self.update_cost()
            if self.nb.index("current") == 3:
                self.log_refresh()
        except tk.TclError:
            return
        try:
            self.root.after(1500, self._poll)
        except tk.TclError:
            pass

    # ---------------------------------------------------------- 关闭

    def on_close(self):
        take = self.cfg.get("takeover") or {}
        if take.get("active"):
            ans = messagebox.askyesnocancel(
                APP_NAME,
                "当前处于接管状态，退出后网关停止，Claude Code 将无法请求。\n\n"
                "是：先还原 Claude Code 配置再退出\n"
                "否：保持接管状态直接退出\n"
                "取消：不退出")
            if ans is None:
                return
            if ans:
                self.restore_config()
        if self.server is not None:
            self.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        for name in (("aqua",) if IS_MAC else ("vista", "winnative")):
            if name in style.theme_names():
                style.theme_use(name)
                break
    except tk.TclError:
        pass
    StudioGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# 打包（在 D:\ai_code\pojia 下执行）：
#   .\.venv\Scripts\python.exe -m PyInstaller -F -w -n PJGateway --noconfirm --runtime-tmpdir ".\runtime" pj_studio.py
#   产物：dist\PJGateway.exe
# ---------------------------------------------------------------------------
