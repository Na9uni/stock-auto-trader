@echo off
chcp 65001 >nul 2>&1
echo ============================================
echo   Stock Auto-Trading System Start
echo ============================================
echo.
echo [1/2] Kiwoom Collector (32-bit)...
start "Collector" cmd /k "cd /d %~dp0 && C:\Python311-32\python.exe -m kiwoom.kiwoom_collector"
timeout /t 5 /nobreak >nul

echo [2/2] Scheduler + Telegram (64-bit)...
start "Scheduler" cmd /k "cd /d %~dp0 && python -c "from alerts.analysis_scheduler import run_scheduler; from alerts.telegram_commander import start_telegram_commander; start_telegram_commander(); run_scheduler()""

echo.
echo System started!
echo   - Collector: separate window
echo   - Scheduler: separate window
echo   - Dashboard: run_dashboard.bat (manual)
echo.
pause
