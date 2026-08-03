# Create desktop shortcuts for the Hana auto-trading bot.
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\create_desktop_shortcut.ps1
# To remove: delete the .lnk files from the Desktop.

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Desktop = [Environment]::GetFolderPath("Desktop")

function New-Shortcut {
    param([string]$Name, [string]$Target, [string]$Icon, [string]$Desc)
    $shell = New-Object -ComObject WScript.Shell
    $path = Join-Path $Desktop "$Name.lnk"
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath       = $Target
    $sc.WorkingDirectory = $ProjectDir
    $sc.IconLocation     = $Icon
    $sc.Description      = $Desc
    $sc.Save()
    Write-Host "Created: $path" -ForegroundColor Green
}

# Windows built-in icons:
#   shell32.dll,167 : green chart
#   shell32.dll,137 : gear
#   shell32.dll,131 : red X

New-Shortcut `
    -Name "하나 자동매매 (포트폴리오)" `
    -Target (Join-Path $ProjectDir "start_portfolio.bat") `
    -Icon "shell32.dll,167" `
    -Desc "Hana auto-trading bot (MGC + MES mock)"

New-Shortcut `
    -Name "하나 자동매매 (단일 종목)" `
    -Target (Join-Path $ProjectDir "start_single.bat") `
    -Icon "shell32.dll,137" `
    -Desc "Hana single-symbol auto-trading bot"

New-Shortcut `
    -Name "하나 자동매매 (강제 종료)" `
    -Target (Join-Path $ProjectDir "stop_bot.bat") `
    -Icon "shell32.dll,131" `
    -Desc "Force kill the bot (last resort when Ctrl+C fails)"

Write-Host ""
Write-Host "Done. Check your Desktop for the three shortcuts." -ForegroundColor Cyan
Write-Host "To remove: delete the .lnk files."
