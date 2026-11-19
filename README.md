# PJ Gateway · 破甲一键通

Claude Code 本地网关与人格注入控制台 —— 在 VS Code 的 Claude Code 与上游 Anthropic 兼容端点之间插一层本地代理，按需替换/追加系统提示词，并提供图形化接管、配置与日志面板。

A local gateway and persona-injection console for Claude Code. It sits between the VS Code Claude Code extension and any Anthropic-compatible upstream, optionally replacing or appending the system prompt, and ships with a GUI for takeover, configuration and live logs.

---

## 中文

### 这是什么

`PJ Gateway` 是一个只依赖 Python 标准库的本地 HTTP 网关（`pj_gateway.py`），加上一个 Tkinter 图形控制台（`pj_studio.py`）。它把 Claude Code 发往 Anthropic `Messages` API 的请求拦截下来，改写 `system` 字段后再转发给上游，SSE 流式响应原样透传，不缓冲、不改包。

典型链路：

```
VS Code · Claude Code 扩展
        │  ANTHROPIC_BASE_URL = http://127.0.0.1:8788
        ▼
PJ Gateway（本程序：注入 / 替换 system、统计 token、写日志）
        │  upstream = http://127.0.0.1:15721 或 https://api.deepseek.com/anthropic
        ▼
cc-switch 本地代理  ──►  上游供应商
```

它解决三件事：

1. **系统提示词可控**：Claude Code 客户端每次会话都会带上自己的系统提示，本网关可按 `passthrough` / `append` / `replace` 三种策略接管该字段，内容放在外部文件里，随时编辑、导入导出。
2. **不打断现有工作流**：cc-switch 负责供应商与 token 管理，本程序只改 `settings.json` 里的 `ANTHROPIC_BASE_URL` 指向自己，其余字段原样保留；cc-switch 重新 attach 覆盖配置时，自带自动重接管守卫。
3. **看得见**：每次请求落一条 JSONL 日志，界面实时显示请求数、错误数、输入/输出/缓存 token 累计，以及系统提示词的字符数与预估 token 开销。

### 主要功能

- **本地网关**：监听 `127.0.0.1`，转发 Anthropic Messages API；支持 `anthropic-version` / `anthropic-beta` 头透传与补全；流式与非流式响应均解析 usage。
- **三种 system 策略**：`passthrough` 透传、`append` 追加、`replace` 整段替换；`append` 模式下追加内容为空时使用全文。
- **人格编辑器**：内置出厂人格（破甲一键通 v8.42 核心），支持从文件读取、保存到文件、导入、导出、清空、恢复出厂。
- **Claude Code 对接**：自动检测 `~/.claude/settings.json`、当前 BASE_URL、token、cc-switch 是否处于本地代理模式；一键「接管到本网关」/「还原配置」。
- **自动重接管**：可选守卫，检测到 cc-switch 覆盖 `settings.json` 后自动改回。
- **运行日志**：实时查看请求日志、错误、耗时与 token 统计，支持清空显示与日志文件。
- **token 成本预估**：配置系统提示词时即时给出字符数、预估 token 与占比提示。
- **跨平台打包**：Windows 用 `build_win.ps1` + PyInstaller 单文件打包；macOS 用 `mac/build_macos.sh` 构建 `.app`。

### 快速开始

**方式一：直接运行打包版（Windows）**

1. 下载 Release 中的 `PJGateway.exe`，放到任意可写目录（不要放 `Program Files`）。
2. 双击运行，在「网关配置」页填写上游地址与 API Key（若上游由 cc-switch 接管，Key 留空即可）。
3. 点击「启动网关」，状态变为监听中。
4. 切到「Claude Code 对接」页，点击「接管到本网关」。
5. 回 VS Code 重开一个 Claude Code 会话。

**方式二：从源码运行**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
python -m pip install pyinstaller   # 仅打包时需要，运行本身零第三方依赖
python pj_studio.py
```

**方式三：纯命令行网关**

```bash
python pj_gateway.py
```

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PJ_PORT` | `8788` | 本地监听端口 |
| `PJ_UPSTREAM` | `https://api.deepseek.com/anthropic` | 上游 Anthropic 兼容端点 |
| `PJ_API_KEY` | 空 | 覆盖客户端鉴权头；留空则透传客户端头 |
| `PJ_SYSTEM_MODE` | `replace` | `passthrough` / `append` / `replace` |
| `PJ_SYSTEM_FILE` | `system_override.txt` | 系统提示词文件 |
| `PJ_SYSTEM_APPEND` | 空 | `append` 模式下直接指定的追加文本 |
| `PJ_LOG` | `gateway.jsonl` | 请求日志路径 |
| `PJ_TIMEOUT` | `600` | 上游超时（秒） |

### 打包

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File .\build_win.ps1
# 产物：dist\PJGateway.exe
```

```bash
# macOS
bash mac/build_macos.sh
# 产物：mac/ 下的 .app 包
```

Windows 打包脚本会把 exe 的完整性级别设为 Medium，否则无法写入 `~/.claude`。

### 目录结构

```
pj_gateway.py        # 网关本体（标准库，无第三方依赖）
pj_studio.py         # Tkinter 图形控制台
pj_persona.py        # 出厂人格文本与元信息
build_win.ps1        # Windows 打包脚本
build_macos.sh       # macOS 打包脚本
run_macos.sh         # macOS 源码启动脚本
mac/                 # macOS 构建目录与说明
doc/                 # 文档资源
```

运行时生成（已在 `.gitignore` 中排除）：`config.json`、`system_override.txt`、`gateway.jsonl`、`build/`、`dist/`。

---

## English

### What it is

`PJ Gateway` is a dependency-free local HTTP gateway (`pj_gateway.py`) plus a Tkinter control console (`pj_studio.py`). It intercepts Claude Code's Anthropic `Messages` API traffic, rewrites the `system` field, and forwards the request upstream — streaming SSE responses are passed through untouched.

```text
VS Code · Claude Code extension
        │  ANTHROPIC_BASE_URL = http://127.0.0.1:8788
        ▼
PJ Gateway (inject / replace system prompt, token accounting, logging)
        │  upstream = http://127.0.0.1:15721 or https://api.deepseek.com/anthropic
        ▼
cc-switch local proxy  ──►  upstream provider
```

It solves three problems:

1. **Controllable system prompt** — choose `passthrough`, `append`, or `replace`. Prompt text lives in an external file you can edit, import and export at will.
2. **No workflow disruption** — cc-switch keeps managing providers and tokens; the gateway only rewrites `ANTHROPIC_BASE_URL` in `settings.json` and leaves every other field intact. An optional guard re-applies the takeover if cc-switch overwrites the file.
3. **Observability** — one JSONL record per request, with live request/error counters, cumulative input/output/cache token usage, and prompt-size estimates in the GUI.

### Features

- Local Anthropic-compatible gateway on `127.0.0.1`, forwarding with header passthrough and usage parsing for both streaming and non-streaming responses.
- Three system-prompt strategies: `passthrough`, `append`, `replace`.
- Persona editor with bundled factory persona, file read/write, import/export, clear and factory reset.
- Claude Code integration: detects `~/.claude/settings.json`, current BASE_URL, token and cc-switch proxy mode; one-click takeover and restore.
- Optional auto re-takeover when cc-switch rewrites the config.
- Live request log and token statistics.
- Token-cost estimate while editing the system prompt.
- Single-file Windows build via PyInstaller, macOS `.app` build script included.

### Quick start

**Prebuilt binary (Windows)**

1. Download `PJGateway.exe` from Releases and place it in a writable directory.
2. Launch it and fill in the upstream URL and API key on the Gateway tab (leave the key empty when cc-switch handles authentication).
3. Click **Start gateway**.
4. On the Claude Code tab, click **Take over**.
5. Reopen a Claude Code session in VS Code.

**From source**

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
python pj_studio.py
```

**Headless gateway**

```bash
python pj_gateway.py
```

Environment variables: `PJ_PORT`, `PJ_UPSTREAM`, `PJ_API_KEY`, `PJ_SYSTEM_MODE`, `PJ_SYSTEM_FILE`, `PJ_SYSTEM_APPEND`, `PJ_LOG`, `PJ_TIMEOUT` (see the table in the Chinese section).

### Build

```powershell
powershell -ExecutionPolicy Bypass -File .\build_win.ps1   # -> dist\PJGateway.exe
```

```bash
bash mac/build_macos.sh                                     # -> macOS .app bundle
```

The Windows script sets the executable's integrity level to Medium so it can write to `~/.claude`.

### Repository layout

```
pj_gateway.py    # gateway core (stdlib only)
pj_studio.py     # Tkinter GUI console
pj_persona.py    # bundled default persona
build_win.ps1    # Windows packaging
build_macos.sh   # macOS packaging
run_macos.sh     # macOS launcher from source
mac/             # macOS build assets
doc/             # documentation assets
```

Runtime artifacts (`config.json`, `system_override.txt`, `gateway.jsonl`, `build/`, `dist/`) are ignored by git.
