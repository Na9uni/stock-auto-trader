@echo off
chcp 65001 >nul 2>&1
title Kiwoom Collector (32-bit)
echo ============================================
echo   Kiwoom Data Collector (32-bit Python)
echo ============================================
echo.
cd /d "%~dp0"
C:\Users\Administrator\AppData\Local\Programs\Python\Python311-32\python.exe -m kiwoom.kiwoom_collector
pause
