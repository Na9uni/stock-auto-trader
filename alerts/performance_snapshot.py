"""손실 한도 초과 시 성과 스냅샷 저장 (MOCK 비교 실험용).

MOCK 단계에서 손실 한도(월/일/연속)가 걸렸을 때, 기존처럼 차단하는 대신:
1. 현재 시점 성과를 스냅샷 파일로 영구 저장
2. 거래는 계속 진행

이후 "차단했다면 여기서 멈췄을 성과" vs "실제 지속 운영 성과"를 비교하여
손실 한도 로직의 실효성을 실측 데이터로 검증한다.

LIVE 단계에서는 호출 안 됨 (trade_executor.py가 OPERATION_MODE 체크로 분기).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("stock_analysis")

_SNAPSHOT_DIR = Path(__file__).parent.parent / "data" / "performance_snapshots"


def save_loss_limit_snapshot(
    kind: str,
    current_loss: int,
    limit_value: int,
    positions: dict | None = None,
    extra: dict | None = None,
) -> Path | None:
    """손실 한도 초과 시점의 성과 스냅샷 저장.

    Args:
        kind: "monthly" | "daily" | "consec"
        current_loss: 현재 누적 손실 금액 (원)
        limit_value: 한도 값 (원)
        positions: 현재 보유 포지션 (auto_positions.json 내용 등)
        extra: 추가 필드 (선택)

    Returns:
        저장된 파일 Path, 실패 시 None
    """
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        path = _SNAPSHOT_DIR / f"snapshot_{stamp}_{kind}.json"

        data = {
            "timestamp": now.isoformat(),
            "kind": kind,
            "mode": "MOCK",  # 호출은 MOCK에서만 (LIVE는 trade_executor에서 차단)
            "current_loss": int(current_loss),
            "limit_value": int(limit_value),
            "exceeded_by": int(current_loss) - int(limit_value),
            "positions": positions or {},
            "note": (
                "이 시점에서 LIVE였다면 신규 매수가 차단됐을 것. "
                "MOCK이므로 거래 지속 — 이후 결과와 비교하여 "
                "손실 한도 로직 실효성 실측 검증."
            ),
        }
        if extra:
            data["extra"] = extra

        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

        logger.info("[스냅샷] %s 한도 초과 기록: %s", kind, path.name)
        return path
    except Exception as e:
        logger.warning("[스냅샷] 저장 실패: %s", e)
        return None


def list_snapshots(kind: str | None = None) -> list[Path]:
    """저장된 스냅샷 파일 목록 (최신순).

    Args:
        kind: "monthly" | "daily" | "consec" (None이면 전체)
    """
    if not _SNAPSHOT_DIR.exists():
        return []
    pattern = f"snapshot_*_{kind}.json" if kind else "snapshot_*.json"
    files = sorted(_SNAPSHOT_DIR.glob(pattern), reverse=True)
    return files


def load_snapshot(path: Path) -> dict | None:
    """스냅샷 1건 로드."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("[스냅샷] 로드 실패 %s: %s", path.name, e)
        return None
