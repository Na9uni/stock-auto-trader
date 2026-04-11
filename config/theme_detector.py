"""테마/주도주 감지 모듈 — kiwoom_data.json 기반 실시간 분석.

Functions:
    detect_volume_surge       : 거래대금 급증 종목 감지 (5일 평균 대비 3배)
    detect_institutional_flow : 외국인/기관 순매수 프록시 (거래량+가격 3일 연속 증가)
    detect_52week_high        : 52주 신고가 근접 종목 (최고가 95% 이상)
    detect_relative_strength  : 상대강도(RS) 랭킹 — IBD/오닐 방식
    detect_theme_leaders      : 종합 주도주 판단 (4개 신호 점수화)
    check_theme_leaders       : 스케줄러 엔트리 — 1시간마다 감지 + 텔레그램 알림
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger("stock_analysis")

# ---------------------------------------------------------------------------
# 쿨다운 — 같은 종목 6시간 내 중복 알림 방지
# ---------------------------------------------------------------------------

_leader_cooldown: dict[str, datetime] = {}
_COOLDOWN_HOURS = 6


def _cooldown_ok(ticker: str) -> bool:
    """주도주 알림 쿨다운 체크."""
    last = _leader_cooldown.get(ticker)
    if last is None:
        return True
    elapsed = (datetime.now() - last).total_seconds() / 3600
    return elapsed >= _COOLDOWN_HOURS


def _update_cooldown(ticker: str) -> None:
    _leader_cooldown[ticker] = datetime.now()


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _get_candles_1d(stock: dict) -> list[dict]:
    """종목 데이터에서 candles_1d 추출. 날짜 오름차순 정렬."""
    candles = stock.get("candles_1d", [])
    if not candles:
        return []
    # date 필드 기준 오름차순 정렬
    try:
        return sorted(candles, key=lambda c: c.get("date", ""))
    except (TypeError, KeyError):
        return candles


# ---------------------------------------------------------------------------
# 1. 거래대금 급증 감지
# ---------------------------------------------------------------------------

def detect_volume_surge(data: dict) -> list[dict]:
    """거래대금 급증 종목 감지 — 평소 대비 3배 이상 거래대금 종목.

    Args:
        data: kiwoom_data.json 전체 dict (data["stocks"] 참조)

    Returns:
        [{"ticker": "005930", "name": "삼성전자", "ratio": 3.5, "value": 150억}, ...]
    """
    stocks = data.get("stocks", {})
    result: list[dict] = []

    for ticker, stock in stocks.items():
        candles = _get_candles_1d(stock)
        if len(candles) < 6:
            continue

        # 오늘 = 마지막 캔들, 직전 5일 = 그 앞 5개
        today = candles[-1]
        prev_5 = candles[-6:-1]

        today_close = today.get("close", 0)
        today_vol = today.get("volume", 0)
        if today_close <= 0 or today_vol <= 0:
            continue

        today_value = today_close * today_vol

        # 5일 평균 거래대금
        avg_values = []
        for c in prev_5:
            c_close = c.get("close", 0)
            c_vol = c.get("volume", 0)
            if c_close > 0 and c_vol > 0:
                avg_values.append(c_close * c_vol)

        if not avg_values:
            continue

        avg_value = sum(avg_values) / len(avg_values)
        if avg_value <= 0:
            continue

        ratio = today_value / avg_value
        if ratio >= 3.0:
            result.append({
                "ticker": ticker,
                "name": stock.get("name", ticker),
                "ratio": round(ratio, 1),
                "value": today_value,
            })

    result.sort(key=lambda x: x["ratio"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# 2. 외국인/기관 순매수 프록시
# ---------------------------------------------------------------------------

def detect_institutional_flow(data: dict) -> list[dict]:
    """외국인/기관 순매수 감지 — kiwoom_data에 foreign_buy/sell 있으면 활용.

    foreign/institutional 데이터가 없으면 거래량+가격 연속 증가를
    기관 매집 프록시 신호로 사용.

    Args:
        data: kiwoom_data.json 전체 dict

    Returns:
        [{"ticker": ..., "name": ..., "signal": "accumulation", "days": 3}, ...]
    """
    stocks = data.get("stocks", {})
    result: list[dict] = []

    for ticker, stock in stocks.items():
        # 1차: 실제 외국인/기관 데이터가 있으면 우선 사용
        foreign_net = stock.get("foreign_buy", 0) - stock.get("foreign_sell", 0)
        inst_net = stock.get("inst_buy", 0) - stock.get("inst_sell", 0)
        if foreign_net > 0 or inst_net > 0:
            result.append({
                "ticker": ticker,
                "name": stock.get("name", ticker),
                "signal": "accumulation",
                "days": 0,
                "foreign_net": foreign_net,
                "inst_net": inst_net,
            })
            continue

        # 2차: 캔들 데이터로 프록시 판단
        candles = _get_candles_1d(stock)
        if len(candles) < 4:
            continue

        # 최근 N일 연속 거래량+가격 증가 체크
        consec_days = 0
        for i in range(len(candles) - 1, 0, -1):
            cur = candles[i]
            prev = candles[i - 1]
            cur_close = cur.get("close", 0)
            prev_close = prev.get("close", 0)
            cur_vol = cur.get("volume", 0)
            prev_vol = prev.get("volume", 0)

            if (cur_close > prev_close > 0) and (cur_vol > prev_vol > 0):
                consec_days += 1
            else:
                break

        if consec_days >= 3:
            result.append({
                "ticker": ticker,
                "name": stock.get("name", ticker),
                "signal": "accumulation",
                "days": consec_days,
            })

    result.sort(key=lambda x: x.get("days", 0), reverse=True)
    return result


# ---------------------------------------------------------------------------
# 3. 52주 신고가 근접
# ---------------------------------------------------------------------------

def detect_52week_high(data: dict) -> list[dict]:
    """52주 신고가 근접 종목 — 현재가가 52주 최고가의 95% 이상.

    candles_1d (약 120개 = 6개월)에서 최고 종가를 구한 뒤,
    현재가가 그 95% 이상이면 모멘텀 리더 후보.

    Args:
        data: kiwoom_data.json 전체 dict

    Returns:
        [{"ticker": ..., "name": ..., "pct_from_high": -2.5}, ...]
    """
    stocks = data.get("stocks", {})
    result: list[dict] = []

    for ticker, stock in stocks.items():
        candles = _get_candles_1d(stock)
        if len(candles) < 20:
            continue

        # 캔들 내 최고 종가
        max_close = max(c.get("close", 0) for c in candles)
        if max_close <= 0:
            continue

        # 현재가: 실시간 current_price 우선, 없으면 마지막 캔들 close
        current_price = stock.get("current_price", 0)
        if current_price <= 0:
            current_price = candles[-1].get("close", 0)
        if current_price <= 0:
            continue

        pct_from_high = ((current_price - max_close) / max_close) * 100

        if current_price >= max_close * 0.95:
            result.append({
                "ticker": ticker,
                "name": stock.get("name", ticker),
                "pct_from_high": round(pct_from_high, 1),
                "max_close": max_close,
                "current_price": current_price,
            })

    result.sort(key=lambda x: x["pct_from_high"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# 4. 상대강도(RS) 랭킹
# ---------------------------------------------------------------------------

def detect_relative_strength(data: dict) -> list[dict]:
    """상대강도(RS) 랭킹 — IBD/오닐 방식. 전 종목 대비 수익률 상위.

    RS = 20일 수익률 * 0.4 + 60일 수익률 * 0.6
    상위 30%를 리더로 판정.

    Args:
        data: kiwoom_data.json 전체 dict

    Returns:
        [{"ticker": ..., "name": ..., "rs_score": 85, "rank": 1}, ...]
    """
    stocks = data.get("stocks", {})
    rs_list: list[dict] = []

    for ticker, stock in stocks.items():
        candles = _get_candles_1d(stock)
        if len(candles) < 20:
            continue

        # 현재가
        current_close = candles[-1].get("close", 0)
        if current_close <= 0:
            continue

        # 20일 수익률
        if len(candles) >= 20:
            close_20d_ago = candles[-20].get("close", 0)
            ret_20d = ((current_close - close_20d_ago) / close_20d_ago * 100
                       if close_20d_ago > 0 else 0.0)
        else:
            ret_20d = 0.0

        # 60일 수익률
        if len(candles) >= 60:
            close_60d_ago = candles[-60].get("close", 0)
            ret_60d = ((current_close - close_60d_ago) / close_60d_ago * 100
                       if close_60d_ago > 0 else 0.0)
        else:
            ret_60d = ret_20d  # 데이터 부족 시 20d로 대체

        # RS raw score = 가중평균 수익률
        rs_raw = ret_20d * 0.4 + ret_60d * 0.6

        rs_list.append({
            "ticker": ticker,
            "name": stock.get("name", ticker),
            "rs_raw": rs_raw,
            "ret_20d": round(ret_20d, 1),
            "ret_60d": round(ret_60d, 1),
        })

    if not rs_list:
        return []

    # 백분위 점수 (0~99) 산출
    rs_list.sort(key=lambda x: x["rs_raw"])
    total = len(rs_list)
    for i, item in enumerate(rs_list):
        item["rs_score"] = int((i / total) * 100) if total > 1 else 50

    # 상위 30% 필터 (rs_score >= 70)
    leaders = [item for item in rs_list if item["rs_score"] >= 70]

    # 점수 내림차순 + 순위 부여
    leaders.sort(key=lambda x: x["rs_score"], reverse=True)
    for rank, item in enumerate(leaders, 1):
        item["rank"] = rank
        # 정리: 불필요 필드 제거
        item.pop("rs_raw", None)

    return leaders


# ---------------------------------------------------------------------------
# 5. 종합 주도주 판단
# ---------------------------------------------------------------------------

def detect_theme_leaders(data: dict) -> list[dict]:
    """종합 테마/주도주 판단 — 4개 신호를 종합 점수화.

    점수 배분:
        volume_surge   = +2
        institutional  = +2
        52week_high    = +3
        rs_top30       = +2
    총점 >= 4 → "주도주 후보"

    Args:
        data: kiwoom_data.json 전체 dict

    Returns:
        점수 내림차순 리스트
        [{"ticker": ..., "name": ..., "score": 7, "near_52high": True, ...}, ...]
    """
    # 4개 감지기 실행
    vol_surges = detect_volume_surge(data)
    inst_flows = detect_institutional_flow(data)
    high_52w = detect_52week_high(data)
    rs_leaders = detect_relative_strength(data)

    # ticker → signal 매핑
    vol_set = {v["ticker"]: v for v in vol_surges}
    inst_set = {i["ticker"]: i for i in inst_flows}
    high_set = {h["ticker"]: h for h in high_52w}
    rs_set = {r["ticker"]: r for r in rs_leaders}

    # 전체 종목 이름 매핑
    stocks = data.get("stocks", {})
    all_tickers = set(vol_set) | set(inst_set) | set(high_set) | set(rs_set)

    scored: list[dict] = []

    for ticker in all_tickers:
        score = 0
        flags: dict[str, bool] = {
            "volume_surge": False,
            "institutional": False,
            "near_52high": False,
            "rs_leader": False,
        }

        if ticker in vol_set:
            score += 2
            flags["volume_surge"] = True

        if ticker in inst_set:
            score += 2
            flags["institutional"] = True

        if ticker in high_set:
            score += 3
            flags["near_52high"] = True

        if ticker in rs_set:
            score += 2
            flags["rs_leader"] = True

        if score < 4:
            continue

        name = stocks.get(ticker, {}).get("name", ticker)
        scored.append({
            "ticker": ticker,
            "name": name,
            "score": score,
            **flags,
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------------------------------------------------------------------
# 6. 스케줄러 엔트리 — 텔레그램 알림
# ---------------------------------------------------------------------------

def check_theme_leaders() -> None:
    """1시간마다: 테마/주도주 감지 -> 텔레그램 알림."""
    from alerts.file_io import load_kiwoom_data
    from alerts.market_guard import is_market_hours

    if not is_market_hours():
        return

    data = load_kiwoom_data()
    if not data:
        return

    leaders = detect_theme_leaders(data)
    if not leaders:
        return

    # 쿨다운 필터 — 같은 종목 6시간 내 중복 알림 방지
    new_leaders = [ld for ld in leaders if _cooldown_ok(ld["ticker"])]
    if not new_leaders:
        return

    from alerts.telegram_notifier import TelegramNotifier
    from alerts.notifications import get_admin_id, CMD_FOOTER

    msg_lines = ["\U0001f3c6 [주도주 감지]"]
    for leader in new_leaders[:5]:  # top 5만
        emoji_tags = ""
        if leader.get("near_52high"):
            emoji_tags += "\U0001f4c8"
        if leader.get("volume_surge"):
            emoji_tags += "\U0001f4b0"
        if leader.get("institutional"):
            emoji_tags += "\U0001f3e6"
        if leader.get("rs_leader"):
            emoji_tags += "\U0001f4aa"

        msg_lines.append(
            f"  {leader['name']} ({leader['ticker']}) "
            f"점수: {leader['score']}/9 {emoji_tags}"
        )

    notifier = TelegramNotifier()
    notifier.send_to_users([get_admin_id()], "\n".join(msg_lines) + CMD_FOOTER)

    # 쿨다운 갱신
    for leader in new_leaders[:5]:
        _update_cooldown(leader["ticker"])

    logger.info("주도주 알림 전송: %d건", len(new_leaders[:5]))
