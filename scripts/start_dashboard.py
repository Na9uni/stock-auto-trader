"""Streamlit Dashboard 진입 스크립트 (64-bit python 전용).

bat 파일에서 `py -3.11 scripts\\start_dashboard.py` 로 호출.
streamlit CLI를 subprocess로 래핑해서 sys.path/cwd를 확실히 세팅.

=== 수정 가이드 ===
- 이 파일은 단순 진입점. 대시보드 UI는 ui/dashboard.py 에서.
- 포트 변경은 아래 DASHBOARD_PORT 상수만 바꾸면 됨.
- 이 파일이 실패하면:
  1) `py -3.11 -m pip show streamlit` 으로 streamlit 설치 확인
  2) `scripts/health_check.py` 실행
  3) docs/TROUBLESHOOTING.md 참고
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = _ROOT / "ui" / "dashboard.py"
DASHBOARD_PORT = 8501

# 64-bit Python 체크
if sys.maxsize <= 2**32:
    sys.stderr.write(
        "[ERROR] Dashboard는 64-bit Python이 필요합니다.\n"
        f"  현재: 32-bit ({sys.executable})\n"
    )
    sys.exit(1)

if not DASHBOARD_PATH.exists():
    sys.stderr.write(f"[ERROR] Dashboard 파일 없음: {DASHBOARD_PATH}\n")
    sys.exit(1)


def main() -> None:
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(DASHBOARD_PATH),
        "--server.port", str(DASHBOARD_PORT),
    ]
    subprocess.run(cmd, cwd=str(_ROOT), check=False)


if __name__ == "__main__":
    main()
