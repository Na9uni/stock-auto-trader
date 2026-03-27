@echo off
title 주식 대시보드
echo ============================================
echo   주식 자동매매 대시보드 (Streamlit)
echo ============================================
echo.

cd /d "%~dp0"
streamlit run ui/dashboard.py --server.port 8501

pause
