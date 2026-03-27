@echo off
echo ============================================
echo   주식 자동매매 시스템 전체 시작
echo ============================================
echo.
echo [1/3] 키움 수집기 (32-bit) 시작...
start "키움 수집기" cmd /k "%~dp0run_collector.bat"
timeout /t 5 /nobreak >nul

echo [2/3] 스케줄러 + 텔레그램 (64-bit) 시작...
start "스케줄러" cmd /k "%~dp0run_scheduler.bat"
timeout /t 3 /nobreak >nul

echo [3/3] 대시보드 시작...
start "대시보드" cmd /k "%~dp0run_dashboard.bat"

echo.
echo 전체 시스템 시작 완료!
echo   - 키움 수집기: 별도 창
echo   - 스케줄러: 별도 창
echo   - 대시보드: http://localhost:8501
echo.
pause
