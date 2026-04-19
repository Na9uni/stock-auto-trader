# MOCK 스윙 테스트 전체 수정 가이드

작성일: 2026-04-17
대상: 아빠 컴퓨터 (이전 가이드 적용 후 추가로 필요한 수정)

---

## 이 문서 목적

MOCK 모드에서 스윙 매매 시뮬레이션을 **정상 동작**시키기 위해 발견된 문제들과 수정 내용.
이전 문서(`AI분석_오판_수정가이드.md`)의 수정만으로는 부족함.

---

## 발견된 추가 문제 6가지

### 문제 1: AI가 "거래량 없다"고 오판 (이전 가이드 참조)

→ 이전 가이드(`AI분석_오판_수정가이드.md`) 그대로 적용.

---

### 문제 2: MOCK인데도 CASH 레짐이라 매수 차단

#### 증상
- 시작 메시지에 "최대 슬롯: 0, 매수 허용: X"
- MOCK인데 가상 매수가 하나도 안 됨

#### 원인
`alerts/trade_executor.py`의 `_calc_trade_amount`에서 레짐별 `max_slots` 체크.
CASH는 `max_slots=0`이라 MOCK에서도 슬롯 0으로 계산됨.

#### 수정
`alerts/trade_executor.py`의 `_calc_trade_amount` 함수:

```python
# 변경 전
effective_max_slots = MAX_SLOTS
try:
    from strategies.regime_engine import get_regime_engine
    _rp = get_regime_engine().params
    effective_max_slots = min(MAX_SLOTS, _rp.max_slots)
except Exception:
    pass

# 변경 후
effective_max_slots = MAX_SLOTS
if not MOCK_MODE:
    try:
        from strategies.regime_engine import get_regime_engine
        _rp = get_regime_engine().params
        effective_max_slots = min(MAX_SLOTS, _rp.max_slots)
    except Exception:
        pass
```

---

### 문제 3: MOCK 매수 직후 CASH 청산으로 강제 매도 (루프)

#### 증상
- 10:44 매수 → 10:45 CASH 청산 → 10:59 매수 → 11:01 청산 ... 반복
- MOCK 손실만 누적됨, 스윙 유지 불가

#### 원인
`alerts/signal_runner.py`에서 CASH 레짐일 때 `_execute_regime_liquidation` 먼저 호출 → MOCK도 매 사이클마다 청산됨.

#### 수정
`alerts/signal_runner.py`의 CASH/DEFENSE 처리 블록:

```python
# 변경 전
if regime == RegimeState.CASH:
    _execute_regime_liquidation(data, engine)
    if _OP_MODE != "MOCK":
        return

# 변경 후
if regime == RegimeState.CASH:
    if _OP_MODE == "MOCK":
        logger.info("[MOCK] CASH 레짐 — 청산 스킵, 가상 매매 평가 계속")
    else:
        _execute_regime_liquidation(data, engine)
        return

if regime == RegimeState.DEFENSE:
    if _OP_MODE == "MOCK":
        logger.info("[MOCK] DEFENSE 레짐 — 축소 스킵, 가상 매매 평가 계속")
    else:
        _execute_defense_cuts(data, engine)
        return
```

---

### 문제 4: MOCK 손실이 LIVE 한도에 영향 (MOCK/LIVE 분리)

#### 증상
MOCK 매도로 가상 손실 2~3번 발생하면 **LIVE 매수 전면 차단**됨 (`is_consec_stoploss_exceeded`).

#### 원인
`record_loss_and_stoploss`가 LIVE/MOCK 구분 없이 같은 `monthly_loss.json`에 기록.

#### 수정

**1) `alerts/file_io.py`** — MOCK 경로 추가 + 함수에 `mock` 파라미터

```python
MONTHLY_LOSS_PATH_MOCK = ROOT / "data" / "monthly_loss_mock.json"

def _monthly_loss_path(mock: bool = False):
    return MONTHLY_LOSS_PATH_MOCK if mock else MONTHLY_LOSS_PATH

# load_monthly_loss, save_monthly_loss에 mock=False 파라미터 추가
```

**2) `alerts/market_guard.py`** — 모든 함수에 `mock=False` 파라미터 추가

```python
def record_loss_and_stoploss(loss_amount: int, mock: bool = False) -> None:
def reset_consec_stoploss(mock: bool = False) -> None:
def is_monthly_loss_exceeded(mock: bool = False) -> bool:
def is_consec_stoploss_exceeded(mock: bool = False) -> bool:
```

**3) 호출 지점** — MOCK 블록에서는 `mock=True` 전달

- `alerts/position_manager.py` 손절/트레일링 MOCK 매도 블록: `mock=True`
- `alerts/signal_runner.py` CASH/EOD 청산 MOCK 블록: `mock=True`
- `alerts/crisis_manager.py` 위기MR MOCK 매도 블록: `mock=True`
- `alerts/trade_executor.py` 한도 체크: `mock=MOCK_MODE`
- `alerts/order_manager.py` LIVE 주문 체결 경로: **그대로 유지** (mock=False 기본값)

---

### 문제 5: MOCK 시드머니가 실제 키움 예수금 기준 (비싼 종목 못 삼)

#### 증상
- 키움 실제 예수금 80만원, 슬롯 10개 → 슬롯당 8만원
- 삼성전자 21만원 1주도 못 사는 상황
- 아빠는 "시드머니 100만원으로 테스트" 기대

#### 수정 1) .env에 MOCK_SEED_MONEY 추가

```
MOCK_SEED_MONEY=1000000
```

#### 수정 2) 비싼 종목 1주 매수 허용 + MAX_ORDER_AMOUNT 상한 제거

`alerts/trade_executor.py`의 `_calc_trade_amount`에 `price` 파라미터 추가:

```python
def _calc_trade_amount(price: int = 0) -> int:
    ...
    # MOCK 모드 + MOCK_SEED_MONEY 설정 시 가상 시드 사용
    if MOCK_MODE:
        mock_seed = int(os.getenv("MOCK_SEED_MONEY", "0") or "0")
        if mock_seed > 0:
            balance = mock_seed
    ...
    amount = balance // free_slots
    # MAX_ORDER_AMOUNT 상한 블록 삭제
    # 비싼 종목 매수 허용
    if price > 0 and amount < price:
        if price <= balance:
            logger.info("[시드머니] 슬롯예산 < 현재가 → 1주 금액으로 증액")
            amount = price
        else:
            return 0
    if amount < 10000:
        return 0
    return amount
```

그리고 `import os` 추가.

호출 지점(`_auto_trade`):

```python
# 변경 전
amount = _calc_trade_amount()
if amount <= 0:
    return
price = int(stock.get("current_price", 0))

# 변경 후
price = int(stock.get("current_price", 0))
if price <= 0:
    return
amount = _calc_trade_amount(price=price)
if amount <= 0:
    return
```

---

### 문제 6: 분할매수 2차 기록 누락 (매매일지 + buy_amount)

#### 증상
- 분할매수 2차가 자동 실행되어 `qty`는 증가하지만
- `buy_amount`는 1차 금액 그대로 (2주인데 1주치 금액 저장)
- `data/trade_journal.csv`에는 1차만 기록되어 통계 왜곡

#### 수정
`alerts/position_manager.py` 분할매수 2차 체결 블록:

```python
# 변경 전
pos["qty"] = int(pos.get("qty", 0)) + split_remaining
pos["buy_price"] = int((pos["buy_price"] * (pos["qty"] - split_remaining) + current_price * split_remaining) / pos["qty"])
pos.pop("split_remaining", None)
pos.pop("split_price", None)
logger.info("[분할매수 2차] ...")

# 변경 후: buy_amount 갱신 + 매매일지 기록 추가
pos["qty"] = int(pos.get("qty", 0)) + split_remaining
pos["buy_price"] = int((pos["buy_price"] * (pos["qty"] - split_remaining) + current_price * split_remaining) / pos["qty"])
pos["buy_amount"] = pos["buy_price"] * pos["qty"]
pos.pop("split_remaining", None)
pos.pop("split_price", None)
logger.info("[분할매수 2차] ...")
try:
    from trading.trade_journal import record_trade
    record_trade(
        ticker=ticker, name=name, side="buy",
        quantity=split_remaining, price=current_price,
        reason="분할매수 2차 (40%)",
        strategy=pos.get("strategy", ""),
        mock=True,
    )
except Exception as e:
    logger.warning("[분할매수 2차] 매매일지 기록 실패: %s", e)
```

---

### 문제 7: 텔레그램 봇 중복 실행 (409 Conflict)

#### 증상
- 양쪽 PC(아빠 PC + 아들 PC)에서 같은 봇 토큰으로 polling
- `stock_analysis.log`에 `409 Conflict: terminated by other getUpdates request` 계속 발생
- 텔레그램 명령(`/상태`, `/도움말` 등) 가끔 씹힘

#### 원인
텔레그램 Bot API는 **같은 토큰으로 한 곳에서만 polling 가능**. 두 PC에서 동시 polling하면 서로 끊음.

#### 수정

**1) `alerts/telegram_commander.py`** — 스위치 추가

```python
def start_telegram_commander() -> None:
    if not _BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN 미설정 — 텔레그램 commander 비활성화")
        return
    enabled = os.getenv("TELEGRAM_COMMANDER_ENABLED", "true").lower() == "true"
    if not enabled:
        logger.info("TELEGRAM_COMMANDER_ENABLED=false — 텔레그램 명령 수신 비활성화")
        return
    t = threading.Thread(target=_polling_loop, daemon=True, name="TelegramCommander")
    t.start()
    logger.info("텔레그램 commander 스레드 시작")
```

**2) 양쪽 PC의 `.env` 설정**

| PC | 설정 |
|----|------|
| 메인 PC (명령 수신 담당, 예: 아들 PC) | `TELEGRAM_COMMANDER_ENABLED=true` |
| 보조 PC (예: 아빠 PC) | `TELEGRAM_COMMANDER_ENABLED=false` |

주의: `false`인 PC도 **알림 송신은 정상 동작**. 명령 수신(`/상태` 등)만 메인 PC에서 처리.

---

### 문제 8: feedparser 패키지 누락

#### 증상
`stock_analysis.log`에 정시마다:
```
ERROR feedparser 패키지가 설치되어 있지 않습니다: pip install feedparser
```

#### 수정
```
pip install feedparser
```

---

## .env 전체 설정 (MOCK 스윙 테스트용)

```
KIWOOM_MOCK_MODE=True
OPERATION_MODE=MOCK
EOD_LIQUIDATION=false
MOCK_SEED_MONEY=1000000
TELEGRAM_COMMANDER_ENABLED=false    # 아빠 PC만 false, 아들 PC는 true
```

---

## 수정 후 확인 순서

1. 두 PC 모두 Collector + Scheduler 완전 종료
2. 각 PC의 `.env` 설정 반영 (특히 `TELEGRAM_COMMANDER_ENABLED` 다르게)
3. `pip install feedparser` (둘 다)
4. `kiwoom/__pycache__/kiwoom_collector.cpython-311.pyc` 삭제 (해당 시)
5. `run_all.bat` 재실행 (양쪽 PC)
6. 5분 후 `logs/stock_analysis.log` 확인:
   - `[보정] 삼성전자 prev_volume: 0 → ...` 라인 보이면 데이터 수집 OK
   - `[MOCK] CASH 레짐 — 청산 스킵` 라인 보이면 루프 수정 반영 OK
   - `409 Conflict` 에러가 없거나 급격히 줄었으면 텔레그램 수정 반영 OK
7. 매수 신호 발생 시 텔레그램 메시지 확인:
   - "AI 분석" 섹션이 전문 용어 없이 쉬운 말로 나옴
   - 비싼 종목(삼성전자 등)도 1주 매수 시도 가능
   - MOCK 매수 후 다음 사이클에 즉시 청산되지 않음

---

## 지금까지 수정한 파일 전체 목록

| 파일 | 변경 |
|------|------|
| `.env` | MOCK 설정, `MOCK_SEED_MONEY`, `TELEGRAM_COMMANDER_ENABLED` 추가 |
| `ai/ai_analyzer.py` | AI 프롬프트 초보자 친화적으로 |
| `alerts/crisis_manager.py` | MOCK 매도 시 `mock=True`, 한도 체크 분리 |
| `alerts/file_io.py` | MOCK 전용 손실 파일 지원 |
| `alerts/market_guard.py` | 모든 함수에 `mock` 파라미터 |
| `alerts/position_manager.py` | 분할매수 2차 매매일지 기록, MOCK 손실 분리 |
| `alerts/signal_runner.py` | CASH/DEFENSE MOCK 스킵, AI 호출 개선 |
| `alerts/telegram_commander.py` | `TELEGRAM_COMMANDER_ENABLED` 스위치 |
| `alerts/trade_executor.py` | 시드머니 재계산, 비싼 종목 1주 허용, MOCK 한도 분리 |
| `kiwoom/kiwoom_collector.py` | 첫 틱 즉시 수집, prev_volume 일봉 보정 |
| `run_all.bat`, `run_collector.bat` | Python 경로 수정 |

---

## 재시작 후 정상 동작 예시

텔레그램 메시지 순서:
1. 시스템 시작 메시지 (운영 모드: MOCK 확인)
2. 레짐 전환 알림 (normal → cash, 매크로 CRISIS)
3. 주도주 감지 메시지
4. 매수 신호 + AI 분석 (쉬운 말)
5. **🛒 [가상 매수 1차]** 체결 메시지
6. 1분 이내 **🛒 [가상 매수 2차]** 체결 메시지 (총 2주로 표시)
7. 다음 사이클에 **바로 청산되지 않음** (이게 핵심)
8. 정기 상태 리포트 (30분마다)
