# alerts/ — 신호 처리 + 매매 실행 + 포지션 관리

**역할**: kiwoom_data.json → 신호 → 매매 → 포지션 관리 → 알림. 64-bit 프로세스의 메인 로직.

## 모듈 흐름 (반드시 이해)

```
analysis_scheduler.py  (메인 루프)
  ├─ signal_runner.py        — 전략 평가 + 신호 처리
  │   ├─ trade_executor.py   — 매수 실행 (MOCK/LIVE)
  │   └─ market_guard.py     — 손실 방어 + 급락 감지
  ├─ position_manager.py     — 손절/트레일링/분할매수 2차
  ├─ order_manager.py        — order_queue.json 관리 (LIVE)
  ├─ crisis_manager.py       — RSI(2) 평균회귀 (ETF)
  ├─ telegram_commander.py   — 명령 수신 (polling)
  ├─ telegram_notifier.py    — 알림 송신
  └─ file_io.py              — 공통 JSON I/O + 손실 파일 분리
```

## 모드별 분기 규칙 (절대)

- `OPERATION_MODE in ("LIVE", "MOCK")` 이면 매매. 그 외 차단.
- **MOCK도 손실 방어 약화 금지** (CASH 청산, max_slots 제약 등 LIVE와 동일)
- MOCK 손실은 `monthly_loss_mock.json`에 기록 (`mock=True` 파라미터)
- LIVE 손실은 `monthly_loss.json`
- `is_whitelisted()`로 매매 차단 (BACKTEST_VERIFIED 7종목)
- `is_watched()`로 신호 감지 (102종목, 매매 X)

## 자주 수정하는 파일

- `signal_runner.py`: 전략 평가 + 알림 (수정 시 `_state.py` 임포트 검증 필수)
- `trade_executor.py`: 매수 결정 + 슬롯/금액 계산
- `position_manager.py`: 보유 종목 손절/트레일링 (분할매수 2차 포함)
- `market_guard.py`: 손실 한도 체크 (`mock` 파라미터 필수 전달)
- `_state.py`: **수정 시 모든 alerts/ 모듈 영향** — 전체 임포트 검증 필수

## 절대 금지 (보호 파일 — hooks가 차단)

- `position_manager.py`, `market_guard.py`, `crisis_manager.py` 직접 수정 (사용자 확인 필수)
- `record_loss_and_stoploss()` 호출 시 mock 파라미터 누락 (LIVE/MOCK 손실 섞임)
- AI 판단 "관망"을 무시하고 "경고 후 진행" 코드 추가 (검증 안 됨)

## 분리 원칙

- 신호 감지 ≠ 매매 실행 ≠ 포지션 관리 (3단계 분리)
- signal_runner는 evaluate 호출만, 매매는 trade_executor에 위임
- 손절/트레일링은 position_manager 단독 책임

## Verification

```bash
# 임포트 (signal_runner는 alerts/ 거의 전체 의존)
python -c "from alerts.analysis_scheduler import run_scheduler"

# 핵심 함수 시뮬레이션
python -c "from alerts.market_guard import is_monthly_loss_exceeded; print(is_monthly_loss_exceeded(mock=True))"
```
