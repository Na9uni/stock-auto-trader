# Strategies Guide

## Strategy Protocol

모든 전략은 `strategies/base.py`의 Protocol을 따름:

```python
class Strategy(Protocol):
    name: str
    def evaluate(self, ctx: MarketContext) -> SignalResult: ...
```

## MarketContext

전략에 전달되는 시장 데이터:
- `ticker`: 종목코드
- `current_price`: 현재가
- `open_price`: 시가
- `daily_df`: 일봉 DataFrame (이동평균, 지표 포함)
- `minute_df`: 5분봉 DataFrame
- `orderbook`: 호가 데이터

## SignalResult

- `signal`: BUY / SELL / NEUTRAL
- `strength`: STRONG / MEDIUM / WEAK
- `reason`: 신호 발생 사유 (문자열)
- `metadata`: 추가 데이터 (dict)

## 변동성 돌파 (VBStrategy)

- 목표가 = 시가 + 전일레인지 × K
- K값: ETF 0.5, 개별주 0.6
- 필터: 시가 > MA10, MA20 > MA60 (상승장), 레인지 >= 0.5%
- 매매시간: 09:10 ~ 14:00

## 합산 거부권 (ScoreStrategy)

- 14개 지표 합산 점수
- 점수 ≤ -3이면 매수 거부
- 단독 매수 불가 (거부권만)

## 새 전략 추가 방법

1. `strategies/` 에 새 파일 생성
2. `Strategy` Protocol 구현
3. `evaluate()` 메서드에서 `SignalResult` 반환
4. `ComboStrategy` 또는 스케줄러에서 연결
5. 백테스트로 검증: `python -m backtest.backtester_v2`
