@echo off
rem ============================================================
rem  Kiwoom Collector 단독 실행 (32-bit Python 전용)
rem ------------------------------------------------------------
rem  수정 시 주의:
rem    - 반드시 py -3.11-32 사용 (키움 OCX는 32-bit만 지원)
rem    - python -c "..." 사용 금지 (이스케이프 버그)
rem    - 실제 로직은 scripts/start_collector.py 에서 호출
rem  문제 생기면: py -3.11 scripts\health_check.py
rem ============================================================
chcp 65001 >nul 2>&1
title Collector
echo ============================================
echo   Kiwoom Data Collector (32-bit)
echo ============================================
echo.
cd /d "%~dp0"
py -3.11-32 scripts\start_collector.py
pause
