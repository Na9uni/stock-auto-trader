# Architecture

## 2-프로세스 구조

```
[32-bit 프로세스]                    [64-bit 프로세스]
kiwoom_collector.py                  analysis_scheduler.py
  ├─ PyQt5 QAxWidget (COM)             ├─ 전략 평가 (ComboStrategy)
  ├─ 30초 폴링 + 실시간 체결            ├─ 포지션 관리
  ├─ kiwoom_data.json 쓰기             ├─ 자동매매 실행
  └─ order_queue.json 읽기 → 주문      ├─ 텔레그램 알림/명령
                                       └─ AI 분석 (Claude API)
         ◄── JSON IPC ──►
```

## IPC 파일

| 파일 | 방향 | 내용 |
|------|------|------|
| `kiwoom_data.json` | 수집기 → 스케줄러 | 실시간 시세, 잔고 |
| `order_queue.json` | 스케줄러 → 수집기 | 매수/매도 주문 |
| `auto_positions.json` | 양방향 | 자동매매 포지션 |

## Atomic Write 패턴

```python
import tempfile, os, json

def atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)  # atomic on same filesystem
```

## Module Dependency Map

```
kiwoom_collector.py
  → data/kiwoom_data.json (atomic write)
    → signal_runner.py (읽기)
      → strategies/* (evaluate 호출)
        → regime_engine.py (4-Mode 레짐 판단)
        → market_guard.py (급락 감지)
      → order_manager.py (주문 생성)
        → trade_executor.py (MOCK/LIVE 실행)
          → position_manager.py (손절/트레일링)
          → trade_journal.py (CSV 기록)
      → notifications.py → telegram_notifier.py (알림)

analysis_scheduler.py = 메인 진입점 (위 전체 오케스트레이션)
_state.py = 공유 설정 (모든 alerts 모듈이 참조)
```

## Strategy Hierarchy

```
AUTO (auto_strategy.py) — 메인 전략, 레짐별 자동 전환
  ├─ 상승장 → VB (vb_strategy.py) — 변동성 돌파 + 거부권
  ├─ 하락장 → Trend (trend_strategy.py) — 추세추종
  ├─ 위기 → CrisisMR (crisis_meanrev.py) — RSI(2) 평균회귀 (ETF 한정)
  └─ 레짐 판단 ← regime_engine.py + macro_regime.py
보조: combo / score / crisis_rotation / momentum_rotation
```

## Key Patterns

- **Config**: `config/trading_config.py` (frozen dataclass, `.env` 로드)
- **Strategy Protocol**: `strategies/base.py` → `evaluate(MarketContext) -> SignalResult`
- **IPC**: `tempfile → os.replace()` atomic write
- **종목 선정**: `config/stock_screener.py` (6겹) + `config/whitelist.py`
  - `is_watched()` 102종목 (감시 대상)
  - `is_whitelisted()` 7종목 (매매 허용 — BACKTEST_VERIFIED)
- **MOCK 독립성**: `monthly_loss_mock.json` / `MOCK_SEED_MONEY` 별도 추적

## Data File Schema

### data/kiwoom_data.json (수집기 → 스케줄러)
```json
{
  "updated_at": "ISO8601",
  "stocks": {
    "<ticker>": {
      "current_price": int,
      "change_rate": float,
      "prev_volume": int,
      "candles_1m": [...],
      "candles_1d": [...]
    }
  },
  "indices": {...},
  "account": {...}
}
```

### data/auto_positions.json
```json
{
  "<ticker>": {
    "qty": int,
    "buy_price": int,
    "buy_amount": int,
    "high_price": int,
    "trailing_activated": bool,
    "rule_name": str,
    "manual": bool,
    "mock": bool
  }
}
```

## Regime Engine (4-Mode)

| 레짐 | max_slots | position_size | stoploss | 매수 | 청산 |
|------|-----------|---------------|----------|------|------|
| NORMAL | 2 | 100% | 2.0% | ✅ | ✅ |
| SWING | 2 | 50% | 1.5% | ✅ | ✅ |
| DEFENSE | 1 | 30% | 1.0% | ❌ | 50% |
| CASH | 0 | 0% | 0.5% | ❌ | 100% |

## 리스크 관리

- 일일 손실 한도: `MAX_DAILY_LOSS`
- 월간 손실 한도: `MAX_MONTHLY_LOSS`
- 연속 손절 한도: `MAX_CONSEC_STOPLOSS`
- 트레일링 스탑: 활성화 → 하락 시 자동 매도

## Related Documents

- 매매 설정: `.env`
- 전략 추가: `.claude/skills/add-strategy.md`
- 도메인 지식: `.claude/skills/domain-knowledge.md`
- 백테스트: `.claude/skills/backtest.md`
- 디렉토리별 규칙: `strategies/CLAUDE.md`, `alerts/CLAUDE.md`, `backtest/CLAUDE.md`
