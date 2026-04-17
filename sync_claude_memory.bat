@echo off
REM Claude Code 세션 종료 직전 실행 — 대화 기록 최신본 새 경로로 싱크
REM 사용법: 이 세션 끝내기 전에 더블클릭

echo ============================================
echo   Claude Code memory/transcript sync
echo ============================================
echo.

set SRC=C:\Users\640jj\.claude\projects\C--Users-640jj-Desktop-stock
set DST=C:\Users\640jj\.claude\projects\C--stock

robocopy "%SRC%" "%DST%" /E /COPY:DAT /R:1 /W:1 /XJ /NP /NDL /NFL

echo.
echo Sync complete. 이제 세션 닫고 C:\stock에서 새로 여세요.
echo.
pause
