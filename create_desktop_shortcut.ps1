# 바탕화면에 "하나 자동매매 봇" 아이콘 자동 생성
# 사용법: PowerShell 에서 실행
#   .\create_desktop_shortcut.ps1
#
# 삭제하려면 바탕화면의 lnk 파일을 지우면 됩니다.

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
    Write-Host "생성: $path" -ForegroundColor Green
}

# 아이콘 매핑 (Windows 표준 라이브러리 아이콘 이용)
#   shell32.dll,167 : 초록 그래프
#   shell32.dll,44  : 톱니바퀴
#   shell32.dll,131 : X 버튼

New-Shortcut `
    -Name "하나 자동매매 (포트폴리오)" `
    -Target (Join-Path $ProjectDir "start_portfolio.bat") `
    -Icon "shell32.dll,167" `
    -Desc "하나증권 다종목 자동매매 봇 (MGC + MES 모의투자)"

New-Shortcut `
    -Name "하나 자동매매 (단일 종목)" `
    -Target (Join-Path $ProjectDir "start_single.bat") `
    -Icon "shell32.dll,137" `
    -Desc "하나증권 단일 종목 자동매매 봇"

New-Shortcut `
    -Name "하나 자동매매 (강제 종료)" `
    -Target (Join-Path $ProjectDir "stop_bot.bat") `
    -Icon "shell32.dll,131" `
    -Desc "봇 강제 종료 (Ctrl+C 안 될 때 최후 수단)"

Write-Host ""
Write-Host "완료. 바탕화면에서 아이콘을 확인하세요." -ForegroundColor Cyan
Write-Host "삭제하려면 lnk 파일을 지우면 됩니다."
