"""Kiwoom Collector 진입 스크립트 (32-bit python 전용).

bat 파일에서 `py -3.11-32 scripts\\start_collector.py` 로 호출.
python -c 대신 이 스크립트를 쓰는 이유: bat 이스케이프 버그 회피.

=== 수정 가이드 ===
- 이 파일은 단순 진입점. 로직 수정은 kiwoom/kiwoom_collector.py 에서.
- 이 파일이 실패하면:
  1) `py -3.11-32 --version` 으로 32-bit Python 설치 확인
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

# 32-bit Python 강제 체크 (키움 OCX는 32-bit 전용)
if sys.maxsize > 2**32:
    sys.stderr.write(
        "[ERROR] Collector는 32-bit Python이 필요합니다.\n"
        f"  현재: 64-bit ({sys.executable})\n"
        "  올바른 실행: py -3.11-32 scripts\\start_collector.py\n"
    )
    sys.exit(1)

from kiwoom.kiwoom_collector import main as _collector_main


def main() -> None:
    _collector_main()


if __name__ == "__main__":
    main()
