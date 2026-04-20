@echo off
rem ============================================================
rem  Stock Auto Trader - 전체 시작 스크립트
rem ------------------------------------------------------------
rem  [0단계] health_check: 환경 자가 진단 (FAIL 시 중단)
rem  [1단계] Collector (32-bit) 창 띄움
rem  [2단계] Scheduler (64-bit) 창 띄움
rem
rem  수정 시 주의 (재발 방지 규칙):
rem    - 각 start 명령은 반드시 별도 bat 파일(run_collector.bat/
rem      run_scheduler.bat)을 호출할 것. 절대 python -c "..." 쓰지 말 것.
rem      (2026-04-20 이스케이프 버그로 장애 발생)
rem    - 32-bit는 py -3.11-32, 64-bit는 py -3.11 로 명시.
rem      그냥 "python" 쓰면 PATH 순서 따라 잘못된 버전 잡힘.
rem    - 창 제목: title Collector / Scheduler (health_check 식별용)
rem  문제 생기면:
rem    1) py -3.11 scripts\health_check.py 실행
rem    2) docs\TROUBLESHOOTING.md 참고
rem ============================================================
chcp 65001 >nul 2>&1
title Stock Auto Trader
echo.
echo ============================================
echo   Stock Auto Trader - 전체 시작
echo ============================================
echo.

echo [0/2] 환경 자가 진단...
py -3.11 "%~dp0scripts\health_check.py" --strict
if errorlevel 1 (
    echo.
    echo [중단] 환경 문제가 있어 시작하지 않습니다.
    echo        위 FAIL 메시지를 해결한 뒤 다시 실행하세요.
    echo        도움말: docs\TROUBLESHOOTING.md
    echo.
    pause
    exit /b 1
)

echo.
echo [1/2] Collector (32-bit) 창 띄우기...
start "Collector" "%~dp0run_collector.bat"
timeout /t 5 /nobreak >nul

echo [2/2] Scheduler (64-bit) 창 띄우기...
start "Scheduler" "%~dp0run_scheduler.bat"

echo.
echo ============================================
echo   시작 완료!
echo ============================================
echo   - Collector 창: 데이터 수집 (닫지 마세요)
echo   - Scheduler 창: 매매 판단 (닫지 마세요)
echo   - Dashboard  : run_dashboard.bat 수동 실행
echo.
echo 텔레그램에서 시작 알림을 확인하세요.
echo.
pause
