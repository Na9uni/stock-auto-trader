# strategies/ — 매매 전략 모듈

**역할**: `MarketContext` 입력 → `SignalResult` 출력. Pure 함수에 가깝게.

## 베이스 (반드시 준수)

- 모든 전략은 `strategies/base.py`의 `Strategy` Protocol 구현
- 핵심 메서드: `evaluate(ctx: MarketContext) -> SignalResult`
- Signal 타입: `BUY` / `SELL` / `NEUTRAL`
- Strength: `STRONG` / `MEDIUM` / `WEAK` (STRONG만 매매 진행)

## 전략 추가 절차

1. `strategies/<name>_strategy.py` 작성 (Protocol 준수)
2. `add-strategy` 스킬 참조 (체크리스트)
3. `auto_strategy.py`의 레짐별 분기에 추가
4. 단위 테스트 `tests/test_strategies.py` 추가
5. **백테스트 필수**: `python -m backtest.backtester_v2`
6. CRISIS 기간(2022) 분리 백테스트로 하락장 성능 확인

## 절대 금지

- 손절/트레일링 파라미터를 전략 내부에 하드코딩 → `regime_engine.RegimeParams` 사용
- 미래 가격 사용 (lookahead bias) — 백테스트에선 `df.iloc[i]`만, `df.iloc[i+1]` 금지
- 매직 넘버 — `config/trading_config.py` 또는 `whitelist.TICKER_K_MAP` 사용
- `evaluate()` 내부에서 파일 I/O / 네트워크 호출 — Pure 유지

## 레짐 시스템 (auto_strategy 진입 시 자동)

- bull (NORMAL + 종목 MA20>MA60): VB + score_veto
- bear (SWING/DEFENSE/CASH 또는 MA20<MA60): trend → crisis_meanrev (ETF만)
- 시스템 레짐 자체는 `strategies/regime_engine.py`가 관리, 전략은 결과만 사용

## 자주 수정하는 파일

- `vb_strategy.py`: 변동성 돌파 (K값, 필터)
- `auto_strategy.py`: 레짐별 전환 로직
- `regime_engine.py`: 4-Mode 전환 임계값 — **수정 시 백테스트 필수**
- `macro_regime.py`: 매크로 점수 가중치 — **수정 시 시장 분석가 리뷰 필수**

## Verification

```bash
# 단위 테스트
python -m pytest tests/test_strategies.py -v

# 통합 백테스트
python -m backtest.backtester_v2          # VB 단독
python -m backtest.backtester_auto         # AUTO 전체 (레짐 포함)
python -m backtest.compare_vb_filters      # VB 필터 효과
```
