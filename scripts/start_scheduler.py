"""Scheduler + Telegram 시작 스크립트 (64-bit python 전용).

bat 파일에서 `py -3.11 scripts\\start_scheduler.py` 로 호출.
python -c 대신 이 스크립트를 쓰는 이유: bat 이스케이프 버그 회피.

=== 수정 가이드 ===
- 이 파일은 단순 진입점. 분석 로직은 alerts/analysis_scheduler.py 에서.
- 텔레그램 명령 수신은 alerts/telegram_commander.py 에서.
- 이 파일이 실패하면:
  1) `py -3.11 --version` 으로 64-bit Python 설치 확인
  2) `scripts/health_check.py` 실행 (환경 진단)
  3) docs/TROUBLESHOOTING.md 참고
"""
from __future__ import annotations

import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가 (scripts/ 하위 실행 대응)
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 64-bit Python 강제 체크 (분석은 메모리 많이 씀)
if sys.maxsize <= 2**32:
    sys.stderr.write(
        "[ERROR] Scheduler는 64-bit Python이 필요합니다.\n"
        f"  현재: 32-bit ({sys.executable})\n"
        "  올바른 실행: py -3.11 scripts\\start_scheduler.py\n"
    )
    sys.exit(1)

from alerts.analysis_scheduler import run_scheduler
from alerts.telegram_commander import start_telegram_commander


def main() -> None:
    start_telegram_commander()
    run_scheduler()


if __name__ == "__main__":
    main()
