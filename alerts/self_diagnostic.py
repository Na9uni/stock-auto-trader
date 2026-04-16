"""시스템 자가 진단 — 시작 시 자동 실행.

모든 핵심 컴포넌트를 점검하고 문제 있으면 텔레그램으로 즉시 알림.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent


def run_diagnostic() -> list[str]:
    """자가 진단 실행. 문제 목록 반환 (비어있으면 정상)."""
    issues = []

    # 1. .env 파일 존재 + 필수 키 확인
    env_path = ROOT / ".env"
    if not env_path.exists():
        issues.append("🚨 .env 파일 없음!")
    else:
        with open(env_path, "r", encoding="utf-8") as f:
            env_content = f.read()
        required_keys = [
            "KIWOOM_ACCOUNT_NUMBER",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
            "STRATEGY",
            "AUTO_TRADE_AMOUNT",
        ]
        for key in required_keys:
            if key not in env_content:
                issues.append(f"⚠️ .env에 {key} 없음")
            elif f"{key}=" in env_content:
                # 값이 비어있는지 확인
                for line in env_content.split("\n"):
                    if line.startswith(f"{key}="):
                        val = line.split("=", 1)[1].strip()
                        if not val:
                            issues.append(f"⚠️ .env {key} 값 비어있음")

    # 2. 핵심 모듈 import 가능한지
    try:
        from config.trading_config import TradingConfig
        config = TradingConfig.from_env()
    except Exception as e:
        issues.append(f"🚨 TradingConfig 로드 실패: {e}")
        return issues  # 이후 진단 불가

    try:
        from strategies.auto_strategy import AutoStrategy
        AutoStrategy(config)
    except Exception as e:
        issues.append(f"🚨 AutoStrategy 생성 실패: {e}")

    try:
        from strategies.regime_engine import RegimeEngine
    except Exception as e:
        issues.append(f"🚨 RegimeEngine import 실패: {e}")

    # 3. 텔레그램 발송 가능한지
    try:
        from alerts.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier()
        # 실제 발송은 안 하고 객체 생성만 확인
        if not config.telegram_bot_token or not config.telegram_chat_id:
            issues.append("⚠️ 텔레그램 설정 불완전")
    except Exception as e:
        issues.append(f"⚠️ 텔레그램 초기화 실패: {e}")

    # 4. data 디렉토리 접근 가능한지
    data_dir = ROOT / "data"
    if not data_dir.exists():
        issues.append("⚠️ data/ 디렉토리 없음")
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            logger.info("[자가진단] data/ 디렉토리 생성")
        except Exception:
            issues.append("🚨 data/ 디렉토리 생성 불가")

    # 5. logs 디렉토리 접근 가능한지
    logs_dir = ROOT / "logs"
    if not logs_dir.exists():
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            issues.append("⚠️ logs/ 디렉토리 생성 불가")

    # 6. auto_positions.json 읽기 가능한지
    positions_path = ROOT / "data" / "auto_positions.json"
    if positions_path.exists():
        try:
            import json
            with open(positions_path, "r", encoding="utf-8") as f:
                positions = json.load(f)
            # selling=True 상태로 멈춘 포지션 확인
            stuck = [
                name for t, p in positions.items()
                if p.get("selling") and not p.get("manual")
                for name in [p.get("name", t)]
            ]
            if stuck:
                issues.append(f"⚠️ 매도 대기 중 포지션: {', '.join(stuck)} (재시작으로 해결 가능)")
        except Exception as e:
            issues.append(f"⚠️ auto_positions.json 읽기 실패: {e}")

    # 7. 설정값 이상 확인
    if config.auto_trade_amount <= 0:
        issues.append("🚨 AUTO_TRADE_AMOUNT가 0 이하")
    if config.max_slots <= 0:
        issues.append("🚨 MAX_SLOTS가 0 이하")
    if config.stoploss_pct <= 0:
        issues.append("🚨 STOPLOSS_PCT가 0 이하")
    if config.max_daily_loss <= 0:
        issues.append("⚠️ MAX_DAILY_LOSS가 0 이하")

    return issues


def run_and_report() -> None:
    """자가 진단 실행 + 텔레그램 결과 발송."""
    logger.info("[자가진단] 시작...")
    issues = run_diagnostic()

    try:
        from alerts.telegram_notifier import TelegramNotifier
        from alerts.notifications import get_admin_id, CMD_FOOTER
        notifier = TelegramNotifier()

        if issues:
            msg = "🔧 [시작 자가 진단] 문제 발견!\n\n"
            for issue in issues:
                msg += f"  {issue}\n"
            msg += "\n확인 필요합니다."
            notifier.send_to_users([get_admin_id()], msg + CMD_FOOTER)
            logger.warning("[자가진단] %d건 문제 발견: %s", len(issues), "; ".join(issues))
        else:
            logger.info("[자가진단] 모든 항목 정상 ✅")
    except Exception as e:
        logger.error("[자가진단] 결과 발송 실패: %s", e)
