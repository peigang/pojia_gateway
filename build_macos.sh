#!/usr/bin/env bash
# 在 macOS 上打包 PJGateway.app
# 用法： chmod +x build_macos.sh && ./build_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

echo "==> 检查 Python / tkinter"
"$PY" -c "import sys, tkinter; print(sys.version.split()[0], 'Tk', tkinter.TkVersion)"

echo "==> 准备虚拟环境"
if [ ! -x .venv-macos/bin/python ]; then
  "$PY" -m venv .venv-macos
fi
. .venv-macos/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install pyinstaller

echo "==> 打包 .app"
rm -rf build dist
python -m PyInstaller --noconfirm --windowed --name PJGateway \
  --osx-bundle-identifier cn.pojiaduan.pjgateway \
  --exclude-module PIL --exclude-module numpy --exclude-module pandas \
  pj_studio.py

echo "==> 临时签名（无开发者证书时用 ad-hoc）"
codesign --force --deep --sign - "dist/PJGateway.app"

echo "==> 打 zip"
ditto -c -k --sequesterRsrc --keepParent "dist/PJGateway.app" "dist/PJGateway-macos.zip"

echo
echo "完成：dist/PJGateway.app"
echo "首次打开若被 Gatekeeper 拦：右键 → 打开，或执行"
echo "  xattr -dr com.apple.quarantine dist/PJGateway.app"
