@echo off
title [64-bit] 주식 분석 스케줄러
echo ============================================
echo   주식 분석 스케줄러 + 텔레그램 봇 (64-bit)
echo ============================================
echo.

cd /d "%~dp0"
python --version
echo 스케줄러 시작...
echo.

python -c "from alerts.analysis_scheduler import run_scheduler; from alerts.telegram_commander import start_telegram_commander; start_telegram_commander(); run_scheduler()"

pause
