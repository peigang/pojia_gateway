# build_win.ps1 —— Windows 一键打包
# 用法：  powershell -ExecutionPolicy Bypass -File .\build_win.ps1
#        .\build_win.ps1 -NoDesktopCopy   不往桌面复制
param(
    [string]$VenvPython = ".\.venv\Scripts\python.exe",
    [switch]$NoDesktopCopy
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path $VenvPython)) {
    throw "找不到 venv python：$VenvPython （先执行 python -m venv .venv 并安装 pyinstaller）"
}

Write-Host "==> 1/6 停止正在运行的实例"
Get-Process -Name PJGateway -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

Write-Host "==> 2/6 编译检查"
& $VenvPython -m py_compile pj_studio.py pj_gateway.py pj_persona.py
if ($LASTEXITCODE -ne 0) { throw "语法检查未通过" }

Write-Host "==> 3/6 清理旧产物（Medium 标签的 exe 会挡住低完整性构建，必须先删）"
Remove-Item .\dist\PJGateway.exe -Force -ErrorAction SilentlyContinue

Write-Host "==> 4/6 PyInstaller"
& $VenvPython -m PyInstaller -F -w -n PJGateway --noconfirm --runtime-tmpdir ".\runtime" pj_studio.py | Out-Null
if ($LASTEXITCODE -ne 0 -or -not (Test-Path .\dist\PJGateway.exe)) { throw "打包失败" }

Write-Host "==> 5/6 修正完整性级别（否则写不进 ~/.claude）"
icacls ".\dist\PJGateway.exe" /setintegritylevel Medium | Out-Null

if (-not $NoDesktopCopy) {
    $desk = [Environment]::GetFolderPath("Desktop")
    $dst = Join-Path $desk "PJGateway"
    New-Item -ItemType Directory -Path $dst -Force | Out-Null
    Copy-Item ".\dist\PJGateway.exe" "$dst\PJGateway.exe" -Force
    if (Test-Path .\dist\system_override.txt) {
        Copy-Item ".\dist\system_override.txt" "$dst\system_override.txt" -Force
    }
    Write-Host "==> 桌面副本：$dst"
}

Write-Host "==> 6/6 结果"
$exe = Get-Item .\dist\PJGateway.exe
$label = icacls .\dist\PJGateway.exe | Select-String 'Mandatory'
$labelText = if ($label) { $label.Line.Trim() } else { "Medium（默认）" }
Write-Host ("    {0}" -f $exe.FullName)
Write-Host ("    {0:N2} MB   {1}" -f ($exe.Length / 1MB), $labelText)
Write-Host "    完成。"
