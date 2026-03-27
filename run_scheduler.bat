@echo off
chcp 65001 >/dev/null 2>&1
title Scheduler + Telegram
echo ============================================
echo   Analysis Scheduler + Telegram Bot
echo ============================================
echo.
cd /d "%~dp0"
python -c "from alerts.analysis_scheduler import run_scheduler; from alerts.telegram_commander import start_telegram_commander; start_telegram_commander(); run_scheduler()"
pause
