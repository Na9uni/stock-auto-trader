# Stock Auto Trader

키움증권 OpenAPI+ 기반 주식 자동매매 시스템

## Architecture

2-프로세스: 32-bit 키움 수집기 + 64-bit 스케줄러/분석
IPC: atomic JSON write (`data/kiwoom_data.json`, `order_queue.json`, `auto_positions.json`)
Scheduler 모듈: `_state.py`(설정) → `signal_runner.py`(신호) → `order_manager.py`(주문) → `analysis_scheduler.py`(진입점)

## Entry Points

- `run_all.bat` — 전체 (수집기 + 스케줄러)
- `run_dashboard.bat` — Streamlit 대시보드
- `python -m backtest.backtester_v2` — 백테스트

## Key Patterns

- Config: `config/trading_config.py` (frozen dataclass, `.env` 로드)
- Strategy: `strategies/base.py` (Protocol: `evaluate(MarketContext) -> SignalResult`)
- AUTO: 상승장=VB, 하락장=추세추종 자동 전환 — `strategies/auto_strategy.py`
- IPC: tempfile → `os.replace()` atomic write

## Conventions

- `from __future__ import annotations` 필수
- Path: `Path(__file__).parent.parent` 기준
- 한글 주석, `_private` prefix, Enum for signals
- JSON 수정 시 반드시 atomic write 패턴

## Verification

```bash
python -m py_compile <file.py>
python -c "from alerts.analysis_scheduler import run_scheduler; print('OK')"
python -m backtest.backtester_v2
```

## Personas

- **아들**: 개발자. 기술적 대화. 간결하게.
- **아빠**: 완전 초보. 한 단계씩. 쉬운 말. `docs/learning/` 참조.

## Agent Roles (PD 체계)

검증 회의: `/expert-meeting` (퀀트/기술적/리스크/시장 전문가 4인)
코드 변경 전 Explore → Plan → 실행 → Verify → Backtest

## Rules

- `.env` 절대 커밋 금지
- 키움 OpenAPI는 32-bit Python만 지원
- 매매 로직 수정 시 반드시 백테스트 실행
- 손실 방어 로직(MAX_DAILY_LOSS 등) 제거/완화 금지
- 대충 작업 금지: 코드 변경 → 검증 → 확인 사이클 준수
- 아빠 작업 시 한 번에 하나의 단계만 안내
