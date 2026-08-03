@echo off
chcp 65001 >nul
setlocal
title Hana 자동매매 봇 (다종목 포트폴리오 - 모의투자)

REM ─────────────────────────────────────────────────────────────
REM  하나증권 다종목 자동매매 봇 실행 스크립트
REM  더블클릭 하면 자동으로 관리자 권한 승격 → 봇 실행.
REM  종료: Ctrl+C 한 번만 (X 로 창 닫지 마세요!)
REM ─────────────────────────────────────────────────────────────

REM 1) 관리자 권한 자동 승격
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 관리자 권한이 필요합니다. UAC 승인 창이 뜨면 예를 눌러주세요...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

REM 2) 프로젝트 폴더로 이동 (이 배치 파일이 있는 곳)
cd /d "%~dp0"

REM 3) 32비트 Python 경로 (필요 시 여기만 수정)
set PY32=C:\Python311-32\python.exe

if not exist "%PY32%" (
    echo.
    echo [오류] 32비트 Python 을 찾을 수 없습니다: %PY32%
    echo 이 배치 파일의 'set PY32=' 라인을 실제 경로로 수정하세요.
    pause
    exit /b 1
)

if not exist "config\config.local.yaml" (
    echo.
    echo [오류] config\config.local.yaml 이 없습니다.
    echo config\config.local.example.yaml 을 복사해서 계정 정보를 채워주세요.
    pause
    exit /b 1
)

REM 4) 실행 안내 + 봇 구동
echo.
echo ================================================================
echo   하나증권 다종목 포트폴리오 자동매매  ^(모의투자^)
echo   종료: Ctrl+C 한 번 만 누르세요 ^(X 버튼으로 창 닫지 마세요!^)
echo ================================================================
echo.

"%PY32%" portfolio_main.py

echo.
echo ── 봇이 종료되었습니다. 아무 키나 누르면 창이 닫힙니다 ──
pause >nul
