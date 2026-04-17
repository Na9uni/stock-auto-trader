"""시스템 자가 진단 — 시작 시 자동 실행.

모든 핵심 컴포넌트를 점검하고 항목별 결과를 텔레그램으로 발송.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("stock_analysis")

ROOT = Path(__file__).parent.parent


def run_diagnostic(send_test_message: bool = False) -> list[tuple[str, bool, str]]:
    """자가 진단 실행. (항목명, 정상여부, 상세) 리스트 반환.

    Args:
        send_test_message: True이면 텔레그램 테스트 메시지를 실제로 발송.
            Heartbeat에서 주기적으로 호출할 때는 False로 설정해 스팸 방지.
            시작 시 1회 진단(run_and_report)에서만 True.
    """
    results = []

    # 1. Python 정상?
    try:
        import sys
        ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        results.append(("Python", True, f"v{ver}"))
    except Exception as e:
        results.append(("Python", False, str(e)))

    # 2. 핵심 라이브러리 있나?
    missing_libs = []
    for lib in ["pandas", "numpy", "schedule", "requests", "yfinance", "dotenv"]:
        try:
            __import__(lib)
        except ImportError:
            missing_libs.append(lib)
    if missing_libs:
        results.append(("라이브러리", False, f"미설치: {', '.join(missing_libs)}"))
    else:
        results.append(("라이브러리", True, "6개 전부 OK"))

    # 3. .env 파일 + 필수 키?
    env_path = ROOT / ".env"
    if not env_path.exists():
        results.append((".env 파일", False, "파일 없음!"))
    else:
        with open(env_path, "r", encoding="utf-8") as f:
            env_content = f.read()
        missing_keys = []
        required = [
            "KIWOOM_ACCOUNT_NUMBER", "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID", "STRATEGY", "AUTO_TRADE_AMOUNT",
        ]
        for key in required:
            if f"{key}=" not in env_content:
                missing_keys.append(key)
            else:
                for line in env_content.split("\n"):
                    if line.startswith(f"{key}=") and not line.split("=", 1)[1].strip():
                        missing_keys.append(f"{key}(비어있음)")
        if missing_keys:
            results.append((".env 파일", False, f"누락: {', '.join(missing_keys)}"))
        else:
            results.append((".env 파일", True, f"필수 키 {len(required)}개 OK"))

    # 4. 핵심 모듈 import?
    try:
        from config.trading_config import TradingConfig
        config = TradingConfig.from_env()
        from strategies.auto_strategy import AutoStrategy
        AutoStrategy(config)
        from strategies.regime_engine import RegimeEngine
        results.append(("매매 모듈", True, "TradingConfig + AutoStrategy + RegimeEngine OK"))
    except Exception as e:
        results.append(("매매 모듈", False, str(e)[:50]))

    # 5. data/logs 디렉토리?
    data_ok = True
    for d in ["data", "logs"]:
        p = ROOT / d
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:
                data_ok = False
    if data_ok:
        results.append(("데이터 폴더", True, "data/ + logs/ OK"))
    else:
        results.append(("데이터 폴더", False, "디렉토리 생성 불가"))

    # 6. 텔레그램 설정/발송 테스트
    # send_test_message=True(시작 1회 진단)일 때만 실제 발송하여 스팸 방지.
    # False일 때는 토큰/chat_id 설정만 검증.
    try:
        from alerts.telegram_notifier import TelegramNotifier
        notifier = TelegramNotifier()
        if not notifier.bot_token or not notifier.default_chat_id:
            results.append(("텔레그램", False, "토큰/chat_id 미설정"))
        elif send_test_message:
            ok = notifier.send_message("🔧 자가 진단 텔레그램 테스트")
            if ok:
                results.append(("텔레그램", True, "발송 성공"))
            else:
                results.append(("텔레그램", False, "발송 실패"))
        else:
            results.append(("텔레그램", True, "설정 OK (발송 스킵)"))
    except Exception as e:
        results.append(("텔레그램", False, str(e)[:50]))

    # 7. 설정값 정상?
    try:
        from config.trading_config import TradingConfig
        config = TradingConfig.from_env()
        config_issues = []
        if config.auto_trade_amount <= 0:
            config_issues.append("매매금액 0")
        if config.max_slots <= 0:
            config_issues.append("슬롯 0")
        if config.stoploss_pct <= 0:
            config_issues.append("손절 0")
        if config_issues:
            results.append(("설정값", False, ", ".join(config_issues)))
        else:
            results.append(("설정값", True,
                          f"매매 {config.auto_trade_amount:,}원 / "
                          f"슬롯 {config.max_slots}개 / "
                          f"손절 {config.stoploss_pct}%"))
    except Exception as e:
        results.append(("설정값", False, str(e)[:50]))

    # 8. 포지션 상태?
    try:
        import json
        pos_path = ROOT / "data" / "auto_positions.json"
        if pos_path.exists():
            with open(pos_path, "r", encoding="utf-8") as f:
                positions = json.load(f)
            auto_count = sum(1 for p in positions.values() if not p.get("manual"))
            manual_count = sum(1 for p in positions.values() if p.get("manual"))
            stuck = [p.get("name", t) for t, p in positions.items()
                     if p.get("selling") and not p.get("manual")]
            if stuck:
                results.append(("포지션", False, f"매도 멈춤: {', '.join(stuck)}"))
            else:
                results.append(("포지션", True, f"자동 {auto_count}개 / manual {manual_count}개"))
        else:
            results.append(("포지션", True, "포지션 없음 (신규)"))
    except Exception as e:
        results.append(("포지션", False, str(e)[:50]))

    # 9. 키움 수집기 살아있나? (kiwoom_data.json 갱신 시각 체크)
    # 장중에는 최근 3분 내 갱신, 장외는 체크 생략(정상).
    try:
        import json
        from datetime import datetime
        kd_path = ROOT / "data" / "kiwoom_data.json"
        if not kd_path.exists():
            results.append(("키움 수집기", False, "kiwoom_data.json 없음 — 수집기 미실행?"))
        else:
            with open(kd_path, "r", encoding="utf-8") as f:
                kd = json.load(f)
            updated_at = kd.get("updated_at", "")
            now = datetime.now()
            is_market_time = (
                now.weekday() < 5
                and (now.hour == 9 or (10 <= now.hour <= 14) or (now.hour == 15 and now.minute <= 30))
            )
            if not updated_at:
                results.append(("키움 수집기", False, "updated_at 없음"))
            else:
                try:
                    upd = datetime.fromisoformat(updated_at.split(".")[0])
                    age = (now - upd).total_seconds()
                    if is_market_time:
                        if age > 180:
                            results.append(("키움 수집기", False, f"갱신 {int(age)}초 전 — 수집기 멈춤?"))
                        else:
                            results.append(("키움 수집기", True, f"정상 (갱신 {int(age)}초 전)"))
                    else:
                        # 장외: 체크 생략 (정상)
                        results.append(("키움 수집기", True, f"장외 (마지막 갱신 {updated_at[:16]})"))
                except ValueError:
                    results.append(("키움 수집기", False, f"시각 파싱 실패: {updated_at[:30]}"))
    except Exception as e:
        results.append(("키움 수집기", False, str(e)[:50]))

    return results


def run_and_report() -> None:
    """자가 진단 실행 + 텔레그램 항목별 결과 발송. (시작 1회용)"""
    logger.info("[자가진단] 시작...")
    results = run_diagnostic(send_test_message=True)

    # 결과 메시지 생성
    lines = ["🔧 [시작 자가 진단 결과]", ""]
    all_pass = True
    for name, ok, detail in results:
        emoji = "✅" if ok else "❌"
        lines.append(f"  {emoji} {name}: {detail}")
        if not ok:
            all_pass = False

    lines.append("")
    if all_pass:
        lines.append("모든 항목 정상! 매매 준비 완료 🎉")
    else:
        lines.append("⚠️ 위 항목 확인 필요!")

    msg = "\n".join(lines)

    try:
        from alerts.telegram_notifier import TelegramNotifier
        from alerts.notifications import get_admin_id, CMD_FOOTER
        TelegramNotifier().send_to_users([get_admin_id()], msg + CMD_FOOTER)
    except Exception as e:
        logger.error("[자가진단] 결과 발송 실패: %s", e)

    if all_pass:
        logger.info("[자가진단] 모든 항목 정상 ✅")
    else:
        for name, ok, detail in results:
            if not ok:
                logger.warning("[자가진단] ❌ %s: %s", name, detail)
