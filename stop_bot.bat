@echo off
chcp 65001 >nul
title Hana 봇 강제 종료

REM ─────────────────────────────────────────────────────────────
REM  Ctrl+C 도 안 먹힐 때 마지막 수단.
REM  주의: 강제 종료는 서버 세션 좀비를 남길 수 있음.
REM       가능하면 봇 창에서 Ctrl+C 를 먼저 시도하세요.
REM ─────────────────────────────────────────────────────────────

echo.
echo 실행 중인 파이썬 봇을 강제 종료합니다.
echo 강제 종료는 하나 서버에 세션 좀비를 남길 수 있어,
echo 다음 실행 시 로그인 실패가 발생할 수 있습니다.
echo.
set /p CONFIRM="정말 강제 종료하시겠습니까? (y/N): "
if /i not "%CONFIRM%"=="y" (
    echo 취소했습니다.
    pause
    exit /b
)

taskkill /F /IM python.exe /T
echo.
echo 완료. 다음 봇 실행 전 1QHTS 로 로그인해서 세션을 정리하시거나
echo 5~15분 대기 후 실행하시는 것을 권장합니다.
pause
