@echo off
rem ============================================================
rem  Streamlit Dashboard 실행 (64-bit Python 전용)
rem ------------------------------------------------------------
rem  수정 시 주의:
rem    - 포트 변경은 scripts/start_dashboard.py 의 DASHBOARD_PORT 상수
rem    - 실제 UI 는 ui/dashboard.py
rem  접속 주소: http://localhost:8501
rem ============================================================
chcp 65001 >nul 2>&1
title Dashboard
echo ============================================
echo   Stock Dashboard (Streamlit, 64-bit)
echo ============================================
echo.
echo 브라우저가 자동으로 열립니다. 포트: 8501
echo.
cd /d "%~dp0"
py -3.11 scripts\start_dashboard.py
pause
