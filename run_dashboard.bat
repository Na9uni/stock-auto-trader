@echo off
chcp 65001 >/dev/null 2>&1
title Dashboard
echo ============================================
echo   Stock Dashboard (Streamlit)
echo ============================================
echo.
cd /d "%~dp0"
streamlit run ui/dashboard.py --server.port 8501
pause
