@echo off
setlocal
title Hana 자동매매 봇 (단일 종목 - 모의투자)

REM ─────────────────────────────────────────────────────────────
REM  단일 종목 자동매매 실행 (config 의 symbol: 블록 기준)
REM  다종목은 start_portfolio.bat 사용
REM ─────────────────────────────────────────────────────────────

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo 관리자 권한이 필요합니다. UAC 승인해주세요...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set PY32=C:\Python311-32\python.exe

if not exist "%PY32%" (
    echo [오류] 32비트 Python 없음: %PY32%
    pause & exit /b 1
)

echo ================================================================
echo   하나증권 단일 종목 자동매매  ^(모의투자^)
echo   종료: Ctrl+C 한 번 ^(X 로 창 닫지 마세요!^)
echo ================================================================
echo.

"%PY32%" main.py

echo.
pause >nul
