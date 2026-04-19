# AI 분석이 "거래하는 사람이 없다"고 오판하는 문제

## 증상

삼성전자처럼 거래량이 엄청 많은 종목인데도 AI가 이렇게 말함:
> "거래하는 사람이 거의 없어서..."
> "체결강도 0.0 및 거래량 배수 NaN은 실질적 거래 부재를..."

---

## 원인 요약

3가지가 동시에 겹쳐서 발생:

1. 키움 API가 전일 거래량을 0으로 보내줌 → 거래량 비교 불가
2. 체결강도(사려는 사람 vs 파는 사람)를 AI한테 안 넘기고 있었음
3. 시스템 시작 후 10분간 일봉 데이터가 없어서 위 보정도 작동 안 함

---

## 추가 이슈: MOCK 모드인데 CASH 레짐이라 매수 차단

### 증상
시작 메시지에 "최대 슬롯: 0, 포지션 비중: 0%"로 나오고, MOCK 모드인데도 가상 매수가 한 건도 안 됨.

### 원인
CASH 레짐은 `max_slots=0`(매수 차단) 설정이 기본. MOCK 모드도 같이 차단됨.

### 수정 파일: `alerts/trade_executor.py`

레짐 max_slots 오버라이드 부분을 찾아서 MOCK 모드 우회 추가:

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

## 수정할 파일 3개

### 파일 1: `kiwoom/kiwoom_collector.py`

#### 수정 A: 첫 틱에서 일봉/계좌/관심종목 즉시 수집

`_tick` 함수 안에서 아래 3곳을 찾아서 `tick_count == 1 or` 추가:

```python
# 변경 전
if tick_count % INTEREST_EVERY_N_TICKS == 0:
if tick_count % ACCOUNT_EVERY_N_TICKS == 0:
if tick_count % DAILY_EVERY_N_TICKS == 0:

# 변경 후 (3줄 모두)
if tick_count == 1 or tick_count % INTEREST_EVERY_N_TICKS == 0:
if tick_count == 1 or tick_count % ACCOUNT_EVERY_N_TICKS == 0:
if tick_count == 1 or tick_count % DAILY_EVERY_N_TICKS == 0:
```

#### 수정 B: 전일 거래량 보정

`_update_stock_data` 함수에서 아래 위치를 찾기:

```python
        if candles_1d is not None:
            updated["candles_1d"] = candles_1d
        self._data["stocks"][ticker] = updated    ← 이 줄 바로 위에 추가
```

이 두 줄 사이에 아래 코드를 넣기:

```python
        # prev_volume 보정: basic/일봉 수집 시점이 달라도 보정되도록 블록 밖에서 처리
        _cd = candles_1d if candles_1d is not None else updated.get("candles_1d", [])
        if updated.get("prev_volume", 0) == 0 and _cd and len(_cd) >= 2:
            _fixed_pv = _cd[1].get("volume", 0)
            updated["prev_volume"] = _fixed_pv
            logger.info("[보정] %s prev_volume: 0 → %s (일봉 기준)", name, f"{_fixed_pv:,}")
```

**주의사항:**
- 이 코드는 반드시 `if basic:` 블록 **바깥**에 넣어야 함
- basic 정보(가격 등)와 일봉은 따로따로 수집됨
- `if basic:` 안에 넣으면 일봉만 수집할 때 보정이 안 됨 (이걸로 한번 실패함)

정확한 위치:
```python
        if candles_1m is not None:
            updated["candles_1m"] = candles_1m
        if candles_1d is not None:
            updated["candles_1d"] = candles_1d
        # ← 여기에 보정 코드 추가
        self._data["stocks"][ticker] = updated
```

---

### 파일 2: `alerts/signal_runner.py`

`_process_signal` 함수에서 `ai.quick_signal_alert(...)` 호출하는 부분을 찾아서 수정:

변경 전:
```python
        ai_result = ai.quick_signal_alert(
            ticker=ticker, name=name,
            price=int(info.get("current_price", 0)),
            change_rate=info.get("change_rate", 0),
            signal_reasons=signal.reasons,
            rsi=_sig_rsi, macd_cross=_sig_macd,
            vol_ratio=_sig_vol,
        )
```

변경 후:
```python
        # vol_ratio가 nan이면 kiwoom_data에서 직접 계산
        import math
        if math.isnan(_sig_vol):
            _cur_vol = info.get("volume", 0)
            _prev_vol = info.get("prev_volume", 0)
            if _prev_vol > 0:
                _sig_vol = _cur_vol / _prev_vol
        _exec = float(info.get("exec_strength", 0.0))

        ai_result = ai.quick_signal_alert(
            ticker=ticker, name=name,
            price=int(info.get("current_price", 0)),
            change_rate=info.get("change_rate", 0),
            signal_reasons=signal.reasons,
            rsi=_sig_rsi, macd_cross=_sig_macd,
            vol_ratio=_sig_vol,
            exec_strength=_exec,
        )
```

---

### 파일 3: `ai/ai_analyzer.py`

`quick_signal_alert` 함수의 프롬프트를 통째로 교체.

핵심 변경:
- 숫자를 AI한테 그대로 주지 않고, 사람 말로 번역해서 전달
- AI한테 "전문 용어 쓰지 마, 초등학생도 알아듣게" 지시

변경 전:
```
거래량 배수: {vol_ratio:.1f}x
체결강도: {exec_strength:.1f}
```

변경 후:
```python
# 데이터를 사람 말로 번역
import math
if math.isnan(vol_ratio) or vol_ratio == 0:
    vol_desc = "거래가 거의 없음"
elif vol_ratio < 0.5:
    vol_desc = "평소보다 거래 적음"
elif vol_ratio < 1.5:
    vol_desc = "평소 수준"
elif vol_ratio < 3.0:
    vol_desc = "평소보다 거래 많음"
else:
    vol_desc = "거래 폭발적"

if exec_strength == 0:
    exec_desc = "사고파는 사람 데이터 없음"
elif exec_strength < 80:
    exec_desc = "파는 사람이 더 많음"
elif exec_strength < 120:
    exec_desc = "사고파는 힘이 비슷함"
else:
    exec_desc = "사려는 사람이 더 많음"
```

그리고 프롬프트에 이 번역된 값을 사용:
```
거래 상황: {vol_desc}
매수세: {exec_desc}
```

---

## 수정 후 확인 방법

### 1단계: 재시작
- Collector 창(32-bit)과 Scheduler 창 **둘 다** 닫기
- `run_all.bat` 다시 실행

### 2단계: 로그 확인
`logs/kiwoom_collector.log`에서 아래 메시지가 나오면 성공:
```
[보정] 삼성전자 prev_volume: 0 → 24,092,884 (일봉 기준)
[보정] KODEX 200 prev_volume: 0 → 22,556,415 (일봉 기준)
...
```
18종목 전부 보정 로그가 나와야 함.

### 3단계: AI 메시지 확인
다음 매수 신호 때 텔레그램 메시지 확인:
- **정상**: "사려는 사람이 많고 거래도 활발해서 사도 괜찮아요"
- **비정상**: "거래하는 사람이 거의 없어서..." → 아직 안 고쳐진 것

### 4단계: 데이터 직접 확인 (선택)
```
data/kiwoom_data.json 열기
→ "prev_volume" 검색
→ 0이 아닌 큰 숫자면 정상
→ 여전히 0이면 수정 위치가 잘못된 것 (if basic 블록 안에 넣지 않았는지 확인)
```

---

## 흔한 실수

| 실수 | 결과 | 해결 |
|------|------|------|
| 보정 코드를 `if basic:` 안에 넣음 | 일봉 수집 시 보정 안 됨 | 블록 밖으로 이동 |
| Scheduler만 재시작 | 수집기가 옛날 코드로 계속 돌아감 | Collector 창도 닫고 재시작 |
| 코드만 수정하고 재시작 안 함 | 당연히 적용 안 됨 | 반드시 재시작 |
| `__pycache__` 캐시 | 드물지만 옛날 코드가 남을 수 있음 | `kiwoom/__pycache__/kiwoom_collector.cpython-311.pyc` 삭제 후 재시작 |
