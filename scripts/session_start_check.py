"""세션 시작 환경 점검. .claude/settings.json의 SessionStart 훅에서 호출.

기존 settings.json 내부 python -c 방식의 문제:
- bash escape + python f-string escape 중첩 → SyntaxError 발생
- JSON 손상 시 파싱 예외로 훅 전체 실패
- 3~4개 subprocess 호출로 느림

이 스크립트는:
- 단일 subprocess (성능 개선)
- 모든 JSON load에 try/except (손상 파일 대응)
- 출력은 stderr (Claude가 컨텍스트로 인식)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _safe_json_load(path: Path) -> dict | None:
    """손상/없음/부분 기록 상태에서도 None 반환으로 조용히 실패."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {path.name} 읽기 실패: {type(exc).__name__}", file=sys.stderr)
        return None


def main() -> None:
    print("[세션 시작 환경 점검]", file=sys.stderr)

    # .env 존재 여부
    if not (ROOT / ".env").exists():
        print("⚠️  .env 파일 없음 — 키움 API/텔레그램 동작 불가", file=sys.stderr)

    # 매크로 override
    macro = _safe_json_load(ROOT / "data" / "macro_override.json")
    if macro is not None:
        print(
            f"매크로 override: war_active={macro.get('war_active')}, "
            f"vkospi={macro.get('vkospi')}",
            file=sys.stderr,
        )

    # 수집기 마지막 업데이트
    kd = _safe_json_load(ROOT / "data" / "kiwoom_data.json")
    if kd is not None:
        print(f"수집기 마지막 업데이트: {kd.get('updated_at', '')}", file=sys.stderr)

    # 레짐 상태
    regime = _safe_json_load(ROOT / "data" / "regime_state.json")
    if regime is not None:
        reason = regime.get("reason", "")[:40]
        print(f"시스템 레짐: {regime.get('state')} (사유: {reason})", file=sys.stderr)

    # 브랜치
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        print(f"현재 브랜치: {branch}", file=sys.stderr)
        if branch == "master":
            print("⚠️  master 직접 작업 금지! son-dev로 전환 필요", file=sys.stderr)
    except Exception:
        pass


if __name__ == "__main__":
    main()
