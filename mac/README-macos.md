# PJ Gateway Studio · macOS 版

把这个目录整个拷到 Mac 上，然后二选一。

## 方式一：直接跑源码（先验证功能）

```bash
cd mac
chmod +x run_macos.sh
./run_macos.sh
```

脚本会检查 tkinter，没有就提示装（`brew install python-tk`，或换 python.org 的官方包）。

## 方式二：打包成 .app

```bash
cd mac
chmod +x build_macos.sh
./build_macos.sh
```

产物：

```
dist/PJGateway.app          ← 双击运行
dist/PJGateway-macos.zip    ← 分发用
```

首次打开提示"无法验证开发者"，右键 → 打开；或执行：

```bash
xattr -dr com.apple.quarantine dist/PJGateway.app
```

## 数据落在哪

| 项 | 路径 |
|---|---|
| 配置 / 日志 / 人格文件 | `~/Library/Application Support/PJGateway/` |
| Claude Code 配置（被接管的对象） | `~/.claude/settings.json` |

`.app` 内部是只读的，所以配置不写在 bundle 里。

## 首次配置

1. 打开应用 → 「网关配置」页
2. 上游：有 cc-switch 就填它的本地代理地址（如 `http://127.0.0.1:15721`）；没有就直连
   `https://api.deepseek.com/anthropic`，并在 API Key 里填自己的 key
3. system 策略默认 `replace`，不用改
4. 保存 → 启动网关 → 「Claude Code 对接」页点「接管到本网关」
5. 重开 Claude Code 会话，发一句普通问题，回复末尾应有 `▣ 破甲一键通 · 工作中`

## 文件说明

| 文件 | 作用 |
|---|---|
| `pj_gateway.py` | 转发引擎（纯标准库，跨平台） |
| `pj_studio.py` | tkinter 图形界面 |
| `pj_persona.py` | 内置默认人格（15,739 字符） |
| `run_macos.sh` | 源码直跑 |
| `build_macos.sh` | 打包 .app |

## 已知差异

- macOS 没有 Windows 的"完整性级别"限制，不需要 `icacls` 那一步
- 提权走 `osascript`（脚本里已处理），不是 `ShellExecuteW`
- 等宽字体 Menlo，主题 aqua
- 计划任务 / 开机自启尚未移植（macOS 对应 launchd，需要的话再加）
