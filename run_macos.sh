#!/usr/bin/env bash
# 在 macOS 上直接从源码运行（不打包）
# 用法： chmod +x run_macos.sh && ./run_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if ! "$PY" -c "import tkinter" 2>/dev/null; then
  echo "当前 Python 没有 tkinter。"
  echo "  · Homebrew:  brew install python-tk"
  echo "  · 或改用 python.org 的官方安装包"
  exit 1
fi

if [ ! -x .venv-macos/bin/python ]; then
  "$PY" -m venv .venv-macos
fi

exec .venv-macos/bin/python pj_studio.py
