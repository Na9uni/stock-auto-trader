@echo off
title [32-bit] 키움 데이터 수집기
echo ============================================
echo   키움 OpenAPI+ 데이터 수집기 (32-bit)
echo ============================================
echo.

REM 32-bit Python 경로 (키움 COM 호환)
set PYTHON32=py -3.11-32

%PYTHON32% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] 32-bit Python 3.11을 찾을 수 없습니다.
    echo   설치: https://www.python.org/downloads/
    echo   py -3.11-32 명령이 작동해야 합니다.
    pause
    exit /b 1
)

echo Python 32-bit 확인 완료.
echo 수집기 시작...
echo.

cd /d "%~dp0"
%PYTHON32% -m kiwoom.kiwoom_collector

pause
