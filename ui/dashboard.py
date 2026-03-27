"""주식 자동매매 대시보드"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent.parent

KIWOOM_DATA = ROOT / "data" / "kiwoom_data.json"
AUTO_POS = ROOT / "data" / "auto_positions.json"
ORDER_QUEUE = ROOT / "data" / "order_queue.json"
MONTHLY_LOSS = ROOT / "data" / "monthly_loss.json"

# ---------------------------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------------------------

st.set_page_config(page_title="주식 자동매매", layout="wide")
st.title("주식 자동매매 대시보드")

# 10초 자동 새로고침
st.markdown('<meta http-equiv="refresh" content="10">', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 데이터 로더
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5)
def load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def load_all() -> tuple[dict, Any, dict, dict]:
    kd = load_json(str(KIWOOM_DATA)) or {}
    pos_raw = load_json(str(AUTO_POS))
    orders = load_json(str(ORDER_QUEUE)) or {}
    monthly = load_json(str(MONTHLY_LOSS)) or {}
    return kd, pos_raw, orders, monthly


# ---------------------------------------------------------------------------
# 포맷 헬퍼
# ---------------------------------------------------------------------------

def _fmt_won(value: float | int) -> str:
    return f"{int(value):,}원"


def _fmt_rate(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def _color_rate(value: float) -> str:
    """수익률에 따른 색상 문자열 반환 (Streamlit markdown용)."""
    if value > 0:
        return f":red[{_fmt_rate(value)}]"
    elif value < 0:
        return f":blue[{_fmt_rate(value)}]"
    return _fmt_rate(value)


def _time_ago(ts: str) -> str:
    """ISO timestamp -> '몇 분 전' 형식."""
    try:
        dt = datetime.fromisoformat(ts)
        diff = int((datetime.now() - dt).total_seconds())
        if diff < 60:
            return f"{diff}초 전"
        elif diff < 3600:
            return f"{diff // 60}분 전"
        elif diff < 86400:
            return f"{diff // 3600}시간 전"
        return dt.strftime("%m/%d %H:%M")
    except (ValueError, TypeError):
        return ts or "-"


# ---------------------------------------------------------------------------
# 포지션 파싱 (dict 형식 / list 형식 모두 지원)
# ---------------------------------------------------------------------------

def _parse_positions(pos_raw: Any) -> list[dict]:
    if not pos_raw:
        return []
    if isinstance(pos_raw, list):
        return pos_raw
    if isinstance(pos_raw, dict):
        # {"positions": [...]} 형식
        if "positions" in pos_raw:
            return pos_raw["positions"]
        # {ticker: {...}} 형식
        result = []
        for ticker, v in pos_raw.items():
            if isinstance(v, dict):
                entry = dict(v)
                entry.setdefault("ticker", ticker)
                result.append(entry)
        return result
    return []


# ---------------------------------------------------------------------------
# 데이터 로드
# ---------------------------------------------------------------------------

kd, pos_raw, orders, monthly = load_all()

account = kd.get("account", {})
stocks_map: dict[str, dict] = kd.get("stocks", {})
updated_at: str = kd.get("updated_at", "")
operation_mode: str = kd.get("operation_mode", os.getenv("OPERATION_MODE", "UNKNOWN"))

positions = _parse_positions(pos_raw)


# ---------------------------------------------------------------------------
# 1. 시스템 상태
# ---------------------------------------------------------------------------

st.subheader("시스템 상태")

col1, col2, col3, col4 = st.columns(4)

with col1:
    mode_color = "green" if operation_mode == "LIVE" else "orange"
    st.metric("운영 모드", operation_mode)

with col2:
    collector_ok = bool(updated_at)
    collector_status = "정상" if collector_ok else "미확인"
    st.metric("수집기", collector_status)

with col3:
    st.metric("보유 종목", f"{len(positions)}개")

with col4:
    st.metric("마지막 업데이트", _time_ago(updated_at) if updated_at else "-")


# ---------------------------------------------------------------------------
# 2. 계좌 요약
# ---------------------------------------------------------------------------

st.subheader("계좌 요약")

balance = account.get("balance", 0)
total_eval = account.get("total_eval", 0)
est_deposit = account.get("est_deposit", 0)

c1, c2, c3 = st.columns(3)
c1.metric("예수금", _fmt_won(balance))
c2.metric("총평가금액", _fmt_won(total_eval))
c3.metric("추정예탁자산", _fmt_won(est_deposit))


# ---------------------------------------------------------------------------
# 3. 보유 포지션
# ---------------------------------------------------------------------------

st.subheader("보유 포지션")

if positions:
    rows = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        name = pos.get("name", stocks_map.get(ticker, {}).get("name", ticker))
        qty = pos.get("qty", pos.get("quantity", 0))
        avg_price = pos.get("avg_price", pos.get("buy_price", 0))

        # 현재가: kiwoom_data stocks에서 매칭 (price 필드 우선, current_price 폴백)
        stock_info = stocks_map.get(ticker, {})
        cur_price = (
            stock_info.get("price")
            or stock_info.get("current_price")
            or pos.get("current_price", 0)
        )

        profit_rate = (
            (cur_price - avg_price) / avg_price * 100
            if avg_price > 0 and cur_price > 0
            else 0.0
        )
        eval_amount = cur_price * qty if cur_price > 0 else avg_price * qty
        selling = pos.get("selling", False)

        rows.append({
            "종목코드": ticker,
            "종목명": name,
            "수량": qty,
            "평균매입가": avg_price,
            "현재가": cur_price if cur_price > 0 else "-",
            "수익률(%)": round(profit_rate, 2),
            "평가금액": eval_amount,
            "상태": "매도중" if selling else "보유",
        })

    pos_df = pd.DataFrame(rows)
    st.dataframe(
        pos_df,
        use_container_width=True,
        column_config={
            "평균매입가": st.column_config.NumberColumn(format="%d원"),
            "현재가": st.column_config.NumberColumn(format="%d원"),
            "수익률(%)": st.column_config.NumberColumn(format="%.2f%%"),
            "평가금액": st.column_config.NumberColumn(format="%d원"),
        },
        hide_index=True,
    )
else:
    st.info("보유 포지션 없음")


# ---------------------------------------------------------------------------
# 4. 감시 종목 테이블
# ---------------------------------------------------------------------------

st.subheader("감시 종목")

if stocks_map:
    watch_rows = []
    for ticker, info in stocks_map.items():
        watch_rows.append({
            "종목코드": ticker,
            "종목명": info.get("name", ticker),
            "현재가": info.get("price", info.get("current_price", 0)),
            "등락률(%)": round(info.get("change_rate", 0.0), 2),
            "거래량": info.get("volume", 0),
            "신호": info.get("signal", "-"),
            "RSI": round(info.get("rsi", float("nan")), 1) if info.get("rsi") is not None else None,
            "업데이트": _time_ago(info.get("updated_at", "")),
        })

    watch_df = pd.DataFrame(watch_rows)
    st.dataframe(
        watch_df,
        use_container_width=True,
        column_config={
            "현재가": st.column_config.NumberColumn(format="%d원"),
            "등락률(%)": st.column_config.NumberColumn(format="%.2f%%"),
            "거래량": st.column_config.NumberColumn(format="%,d"),
        },
        hide_index=True,
    )
else:
    st.info("감시 종목 데이터 없음")


# ---------------------------------------------------------------------------
# 5. 최근 주문 이력
# ---------------------------------------------------------------------------

st.subheader("최근 주문 이력 (최근 20건)")

order_list: list[dict] = orders.get("orders", []) if isinstance(orders, dict) else []

if order_list:
    recent_orders = order_list[-20:][::-1]  # 최신순

    order_rows = []
    for o in recent_orders:
        action = o.get("action", o.get("order_type", "-"))
        if isinstance(action, int):
            action = "매수" if action == 1 else "매도"

        status = o.get("status", "-")
        status_map = {
            "pending": "대기",
            "submitted": "접수",
            "executed": "체결",
            "failed": "실패",
            "cancelled": "취소",
        }

        ts = o.get("executed_at") or o.get("created_at") or o.get("failed_at") or ""
        order_rows.append({
            "종목코드": o.get("ticker", "-"),
            "구분": action,
            "수량": o.get("quantity", o.get("qty", 0)),
            "주문가": o.get("price", 0),
            "체결가": o.get("exec_price", "-"),
            "상태": status_map.get(status, status),
            "출처": o.get("source", "-"),
            "시각": _time_ago(ts) if ts else "-",
        })

    order_df = pd.DataFrame(order_rows)
    st.dataframe(
        order_df,
        use_container_width=True,
        column_config={
            "주문가": st.column_config.NumberColumn(format="%d원"),
        },
        hide_index=True,
    )
else:
    st.info("주문 이력 없음")


# ---------------------------------------------------------------------------
# 6. 기술적 지표 차트
# ---------------------------------------------------------------------------

st.subheader("기술적 지표 차트")

chart_tickers = list(stocks_map.keys())

if chart_tickers:
    selected = st.selectbox(
        "종목 선택",
        options=chart_tickers,
        format_func=lambda t: f"{t} {stocks_map[t].get('name', '')}",
    )

    if selected:
        stock_info = stocks_map[selected]

        # candles_1m 우선, 없으면 candles_1d
        candles: list[dict] = (
            stock_info.get("candles_1m")
            or stock_info.get("candles_5m")
            or stock_info.get("candles_1d")
            or []
        )

        if candles:
            candle_df = pd.DataFrame(candles)

            # 날짜/시간 컬럼 정규화
            time_col = next(
                (c for c in ("date", "time", "datetime", "dt") if c in candle_df.columns),
                None,
            )
            if time_col:
                candle_df = candle_df.rename(columns={time_col: "시각"})
                candle_df = candle_df.set_index("시각")

            # 종가 차트
            if "close" in candle_df.columns:
                st.line_chart(candle_df["close"], use_container_width=True)
            elif "price" in candle_df.columns:
                st.line_chart(candle_df["price"], use_container_width=True)

            # 거래량 차트
            if "volume" in candle_df.columns:
                st.bar_chart(candle_df["volume"], use_container_width=True, height=150)
        else:
            st.info(f"[{selected}] 캔들 데이터 없음")

        # 현재 지표 요약
        with st.expander("지표 요약"):
            ic1, ic2, ic3, ic4 = st.columns(4)
            ic1.metric("현재가", _fmt_won(stock_info.get("price", stock_info.get("current_price", 0))))
            ic2.metric("등락률", _fmt_rate(stock_info.get("change_rate", 0.0)))
            ic3.metric("RSI", f"{stock_info.get('rsi', 0.0):.1f}" if stock_info.get("rsi") else "-")
            ic4.metric("신호", stock_info.get("signal", "-"))
else:
    st.info("감시 종목 데이터 없음")


# ---------------------------------------------------------------------------
# 7. 손실 관리 상태
# ---------------------------------------------------------------------------

st.subheader("손실 관리")

if monthly:
    lc1, lc2, lc3, lc4 = st.columns(4)

    monthly_loss_amt = monthly.get("monthly_loss", monthly.get("total_loss", 0))
    monthly_loss_rate = monthly.get("monthly_loss_rate", monthly.get("loss_rate", 0.0))
    consecutive_loss = monthly.get("consecutive_loss", monthly.get("consecutive_stop_loss", 0))
    trading_halted = monthly.get("trading_halted", monthly.get("halted", False))

    lc1.metric("월간 손실금액", _fmt_won(abs(monthly_loss_amt)))
    lc2.metric("월간 손실률", f"{abs(monthly_loss_rate):.2f}%")
    lc3.metric("연속 손절 횟수", f"{consecutive_loss}회")
    lc4.metric("거래 중단", "중단" if trading_halted else "정상")

    # 추가 필드 표시
    extra_keys = {
        k: v for k, v in monthly.items()
        if k not in (
            "monthly_loss", "total_loss", "monthly_loss_rate", "loss_rate",
            "consecutive_loss", "consecutive_stop_loss", "trading_halted", "halted",
        )
        and not isinstance(v, (dict, list))
    }
    if extra_keys:
        with st.expander("상세"):
            st.json(extra_keys)
else:
    st.info("손실 관리 데이터 없음 (monthly_loss.json)")
