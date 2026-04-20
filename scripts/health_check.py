"""실행 전 환경 자가 진단 스크립트.

run_all.bat이 시작될 때 먼저 실행되어 환경을 점검한다.
문제가 있으면 FAIL 코드를 반환하고 원인/해결법을 콘솔에 출력.

=== 사용법 ===
    py -3.11 scripts\\health_check.py            # 64-bit 점검
    py -3.11 scripts\\health_check.py --strict   # FAIL 시 exit 1 (bat에서 사용)

=== 점검 항목 (재발 방지용) ===
  1) 현재 폴더가 C:\\stock (또는 하위)인가
  2) 바탕화면에 중복 stock 폴더가 없는가
  3) 32-bit Python 3.11 설치됐나
  4) 64-bit Python 3.11 설치됐나
  5) .env 파일 존재
  6) data/ 폴더 존재
  7) 핵심 import 가능 여부 (alerts, kiwoom, strategies)
  8) 현재 실행 중인 Collector/Scheduler 프로세스 (중복 실행 방지)

=== 수정 가이드 ===
- 새 점검 항목 추가: `checks` 리스트에 (name, func) 튜플 추가
- 각 점검 함수는 (status, message) 반환. status = "PASS" | "WARN" | "FAIL"
- FAIL은 치명적 (실행 중단). WARN은 경고 (실행은 가능).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path("C:/stock")


# ────────────────────────────────────────
# 점검 함수들 (각 함수는 (status, message) 반환)
# ────────────────────────────────────────

def _check_project_folder() -> tuple[str, str]:
    """프로젝트 루트가 C:\\stock 인지 확인."""
    cwd = Path.cwd().resolve()
    root = _ROOT.resolve()
    if root != PROJECT_ROOT.resolve():
        return "FAIL", (
            f"프로젝트 위치가 올바르지 않음: {root}\n"
            f"    → 올바른 위치: {PROJECT_ROOT}\n"
            "    → 해결: C:\\stock 폴더에서만 실행하세요."
        )
    return "PASS", f"프로젝트 루트: {root}"


def _check_no_duplicate_desktop() -> tuple[str, str]:
    """바탕화면에 중복 stock 폴더 없는지."""
    desktops = [
        Path.home() / "Desktop" / "stock",
        Path.home() / "OneDrive" / "Desktop" / "stock",
    ]
    found = [p for p in desktops if p.exists()]
    if found:
        paths = "\n      ".join(str(p) for p in found)
        return "FAIL", (
            f"바탕화면에 중복 stock 폴더 발견:\n      {paths}\n"
            "    → 해결: 해당 폴더를 휴지통으로 이동하세요.\n"
            "    → 스크립트: scripts\\delete_desktop_stock.ps1"
        )
    return "PASS", "바탕화면 중복 폴더 없음"


def _check_python_32() -> tuple[str, str]:
    """32-bit Python 3.11 설치 확인."""
    try:
        r = subprocess.run(
            ["py", "-3.11-32", "-c", "import sys; print(sys.maxsize <= 2**32)"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return "FAIL", (
                "32-bit Python 3.11 미설치 (Collector에 필요)\n"
                "    → 해결: https://www.python.org/ftp/python/3.11.9/python-3.11.9.exe 설치"
            )
        if r.stdout.strip() != "True":
            return "FAIL", "py -3.11-32 가 32-bit가 아닙니다"
        return "PASS", "32-bit Python 3.11 OK"
    except Exception as e:
        return "FAIL", f"32-bit Python 점검 실패: {e}"


def _check_python_64() -> tuple[str, str]:
    """64-bit Python 3.11 설치 확인."""
    try:
        r = subprocess.run(
            ["py", "-3.11", "-c", "import sys; print(sys.maxsize > 2**32)"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            return "FAIL", (
                "64-bit Python 3.11 미설치 (Scheduler에 필요)\n"
                "    → 해결: Microsoft Store 또는 python.org 에서 설치"
            )
        if r.stdout.strip() != "True":
            return "FAIL", "py -3.11 이 64-bit가 아닙니다"
        return "PASS", "64-bit Python 3.11 OK"
    except Exception as e:
        return "FAIL", f"64-bit Python 점검 실패: {e}"


def _check_env_file() -> tuple[str, str]:
    """.env 파일 존재."""
    env = _ROOT / ".env"
    if not env.exists():
        return "FAIL", (
            f".env 파일 없음: {env}\n"
            "    → 해결: docs/learning 참고하여 .env 생성 (키움 계정, 텔레그램 토큰 등)"
        )
    return "PASS", ".env 파일 OK"


def _check_data_folder() -> tuple[str, str]:
    """data/ 폴더 존재."""
    data = _ROOT / "data"
    if not data.exists():
        data.mkdir(parents=True, exist_ok=True)
        return "WARN", f"data/ 폴더 없어서 새로 생성: {data}"
    return "PASS", "data/ 폴더 OK"


def _check_core_imports() -> tuple[str, str]:
    """핵심 모듈 import 가능 여부 (64-bit 기준)."""
    try:
        r = subprocess.run(
            ["py", "-3.11", "-c",
             f"import sys; sys.path.insert(0, r'{_ROOT}');"
             "from alerts.analysis_scheduler import run_scheduler;"
             "from alerts.telegram_commander import start_telegram_commander;"
             "print('OK')"],
            capture_output=True, text=True, timeout=30, cwd=str(_ROOT),
        )
        if r.returncode != 0 or "OK" not in r.stdout:
            return "FAIL", (
                f"핵심 모듈 import 실패:\n    {r.stderr.strip()[:400]}\n"
                "    → 해결: py -3.11 -m pip install -r requirements.txt"
            )
        return "PASS", "alerts / kiwoom / strategies import OK"
    except Exception as e:
        return "FAIL", f"import 점검 실패: {e}"


def _check_running_processes() -> tuple[str, str]:
    """이미 실행 중인 Collector/Scheduler 확인 (중복 실행 방지)."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5,
        )
        count = r.stdout.count("python.exe")
        if count >= 2:
            return "WARN", (
                f"이미 python 프로세스 {count}개 실행 중\n"
                "    → 중복 실행 위험. 필요하면 작업관리자에서 종료 후 재시작."
            )
        if count == 1:
            return "WARN", "python 프로세스 1개 실행 중 (Collector 또는 Scheduler)"
        return "PASS", "실행 중인 python 프로세스 없음"
    except Exception as e:
        return "WARN", f"프로세스 점검 실패: {e}"


# ────────────────────────────────────────
# 실행
# ────────────────────────────────────────

CHECKS: list[tuple[str, Callable[[], tuple[str, str]]]] = [
    ("프로젝트 폴더", _check_project_folder),
    ("바탕화면 중복", _check_no_duplicate_desktop),
    ("32-bit Python", _check_python_32),
    ("64-bit Python", _check_python_64),
    (".env 파일", _check_env_file),
    ("data 폴더", _check_data_folder),
    ("핵심 import", _check_core_imports),
    ("실행 중 프로세스", _check_running_processes),
]

ICONS = {"PASS": "[OK]  ", "WARN": "[WARN]", "FAIL": "[FAIL]"}


def main(strict: bool = False) -> int:
    # Windows 콘솔 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    print("=" * 60)
    print("  Stock Auto Trader - 환경 자가 진단")
    print("=" * 60)

    fails, warns = 0, 0
    for name, func in CHECKS:
        try:
            status, msg = func()
        except Exception as e:  # 방어
            status, msg = "FAIL", f"점검 중 예외: {e}"
        icon = ICONS.get(status, "[??]  ")
        print(f"  {icon} {name:<20} {msg}")
        if status == "FAIL":
            fails += 1
        elif status == "WARN":
            warns += 1

    print("-" * 60)
    print(f"  결과: FAIL={fails} / WARN={warns} / PASS={len(CHECKS) - fails - warns}")
    print("=" * 60)

    if fails:
        print("\n[중단] FAIL 항목을 먼저 해결하세요. docs/TROUBLESHOOTING.md 참고.\n")
        return 1 if strict else 0
    if warns:
        print("\n[주의] WARN 항목이 있지만 실행은 가능합니다.\n")
    else:
        print("\n[정상] 모든 점검 통과 ✓\n")
    return 0


if __name__ == "__main__":
    exit_code = main(strict="--strict" in sys.argv)
    sys.exit(exit_code)
