@echo off
chcp 65001 >/dev/null 2>&1
title Kiwoom Collector (32-bit)
echo ============================================
echo   Kiwoom Data Collector (32-bit Python)
echo ============================================
echo.
cd /d "%~dp0"
py -3.11-32 -m kiwoom.kiwoom_collector
pause
