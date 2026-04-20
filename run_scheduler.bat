@echo off
rem ============================================================
rem  Scheduler + Telegram 단독 실행 (64-bit Python 전용)
rem ------------------------------------------------------------
rem  수정 시 주의:
rem    - 반드시 py -3.11 사용 ("python" 만 쓰면 32-bit로 잡힐 수 있음)
rem    - python -c "..." 사용 금지 (이스케이프 버그, 2026-04-20 장애 원인)
rem    - 실제 로직은 scripts/start_scheduler.py 에서 호출
rem  문제 생기면: py -3.11 scripts\health_check.py
rem ============================================================
chcp 65001 >nul 2>&1
title Scheduler
echo ============================================
echo   Analysis Scheduler + Telegram Bot (64-bit)
echo ============================================
echo.
cd /d "%~dp0"
py -3.11 scripts\start_scheduler.py
pause
