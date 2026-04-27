Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = Split-Path -Parent $ScriptDir
$Edition = "full"
if ($args.Count -gt 0 -and $args[0] -eq "--lite") {
    $Edition = "lite"
}

Write-Host "=== 安装 Lumina (Windows) ==="
Write-Host "项目目录: $ProjectDir"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "未检测到 uv，开始安装..."
    irm https://astral.sh/uv/install.ps1 | iex
    $env:Path += ";$HOME\.local\bin"
}

Set-Location $ProjectDir
Write-Host "正在安装依赖..."
if ($Edition -eq "full") {
    uv sync --extra full
} else {
    uv sync
}

Write-Host "正在配置 SendTo 右键集成..."
$SendToDir = Join-Path $env:APPDATA "Microsoft\Windows\SendTo"
New-Item -ItemType Directory -Force -Path $SendToDir | Out-Null

function Write-SendToFile {
    param(
        [string]$Name,
        [string]$Action
    )

    $Path = Join-Path $SendToDir $Name
    $Content = @"
@echo off
cd /d "$ProjectDir"
uv run python "$ProjectDir\scripts\lumina_file_action.py" $Action %*
pause
"@
    Set-Content -Path $Path -Value $Content -Encoding ASCII
}

Write-SendToFile -Name "Lumina Translate PDF.cmd" -Action "translate"
Write-SendToFile -Name "Lumina Summarize PDF.cmd" -Action "summarize"
Write-SendToFile -Name "Lumina Polish Text.cmd" -Action "polish"

Write-Host ""
Write-Host "✓ 依赖和右键集成安装完成"
Write-Host "右键快捷操作已安装至 SendTo 菜单:"
Write-Host "  Lumina Translate PDF.cmd"
Write-Host "  Lumina Summarize PDF.cmd"
Write-Host "  Lumina Polish Text.cmd"
Write-Host ""
Write-Host "启动服务："
if ($Edition -eq "full") {
    Write-Host "  `$env:LUMINA_EDITION='full'; uv run lumina server"
} else {
    Write-Host "  uv run lumina server --provider openai"
}
Write-Host ""
Write-Host "运行 smoke 检查："
Write-Host "  uv run python scripts/smoke_check.py"
