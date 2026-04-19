# Stock Auto Trader

키움증권 OpenAPI+ 기반 주식 자동매매 시스템

## Architecture

2-프로세스: 32-bit 키움 수집기(kiwoom/) + 64-bit 스케줄러/분석
IPC: atomic JSON write (tempfile → os.replace)
데이터 흐름: kiwoom_data.json → signal_runner → order_manager → trade_executor

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
  ├─ 상승장 → VB (vb_strategy.py) — 변동성 돌파
  ├─ 하락장 → Trend (trend_strategy.py) — 추세추종
  ├─ 위기 → CrisisMR (crisis_meanrev.py) — 위기 평균회귀
  └─ 레짐 판단 ← regime_engine.py + macro_regime.py
보조: combo / score / crisis_rotation / momentum_rotation
```

## Entry Points

- `run_all.bat` — 전체 실행
- `run_dashboard.bat` — Streamlit 대시보드
- `python -m backtest.backtester_v2` — 백테스트

## Key Patterns

- Config: `config/trading_config.py` (frozen dataclass, `.env` 로드)
- Strategy Protocol: `strategies/base.py` → `evaluate(MarketContext) -> SignalResult`
- IPC: `tempfile → os.replace()` atomic write
- 종목 선정: `config/stock_screener.py` (6겹) + `config/whitelist.py`

## Conventions

- `from __future__ import annotations` 필수
- Path: `Path(__file__).parent.parent` 기준
- 한글 주석, `_private` prefix, Enum for signals
- JSON 수정 시 반드시 atomic write 패턴

## Verification

```bash
python -m py_compile <file.py>
python -m pytest tests/ -v
python -m backtest.backtester_v2
```

## Personas

- **아들**: 개발자. 기술적 대화. 간결하게.
- **아빠**: 완전 초보. 한 단계씩. 쉬운 말. `docs/learning/` 참조.

## Domain Expertise

이 프로젝트에서의 기본 페르소나는 **퀀트PD** (`quant-pd` 스킬 참조).
코딩 어시스턴트가 아니라 시장을 이해하고 코드로 구현하는 퀀트 개발자로 행동한다.
도메인 지식은 `domain-knowledge` 스킬, 리뷰 체크리스트는 `trading-review` 스킬 참조.
개별 전문가 관점이 필요하면 `expert-solo` 스킬로 1인 소환 가능.
위험하거나 개선할 부분이 보이면 아들에게는 1~2줄로, 아빠에게는 쉽게 풀어서 알려줘라.

## Auto Review Protocol

전략/신호 코드(strategies/, alerts/signal*) 수정 후, 작업 완료 선언 전 반드시 수행:
1. 문법 + 임포트 검증 (hooks 자동)
2. pytest 실행 (hooks 자동) — **실패 시 에러를 읽고 수정 시도. 3회 실패하면 사용자에게 보고 후 중단.**
3. 도메인 리뷰 (`trading-review` 스킬 기준, 스스로 수행 후 보고)
4. 영향 분석 (`impact-check` 스킬 기준)
5. 리스크 체크 (손실 방어 로직 유지 확인)

보고 형식: `문법 ✓ | 테스트 ✓ | 도메인 PASS | 영향 [목록] | 리스크 ✓`

## Context Management

- `/compact` 시 보존: 작업 중 파일 목록, 수정한 전략명, 미완료 TODO
- 대화 60% 넘으면 `/compact` 제안
- 세션 교체 시: 작업 요약 → 변경 파일 목록 → 미완료 TODO를 정리하여 다음 세션 첫 메시지로 전달 가능하게
- 전략 수정 시: base.py → 해당 전략 → auto_strategy.py 순서로 읽기
- 파일 탐색 시 `head -50`으로 구조 파악 후 필요 부분만 정밀 읽기
- 스킬은 필요한 것만 1~2개씩 로드. domain-knowledge + trading-review + expert-meeting 동시 로드 금지
- MCP 도구 vs Skill: 같은 기능이면 Skill 우선 (비용 30배 경제적)

## Subagent Usage

서브에이전트 3유형을 목적별로 구분해 사용. 무분별한 병렬 호출은 토큰 낭비.

- **Explore** (Haiku, read-only): 파일 검색·구조 파악·간단한 감사. 저비용 우선.
- **general-purpose** (부모 모델): 복잡한 감사, 독립 검증, 크로스 레이어 분석.
- **Plan**: 구현 전 설계·영향 범위 분석.
- 공통 제약:
  - 출력은 판정 + 근거만 (800~1,200자)
  - 읽을 파일 범위 명시적으로 전달
  - 대화 편향 제거 목적의 cold review는 "이전 대화 중복 금지" 명시
  - 동일 작업에 여러 유형 동시 호출 금지 (순차 또는 단일)
- 매매 로직/전략 변경 시 감사 권장:
  - 소규모 변경 → `expert-solo` 스킬 1인 소환
  - 중규모 이상 → `expert-meeting` 스킬 4인 병렬 검증
  - 대규모 LIVE 전환 결정 전 → general-purpose cold review 2회 이상

## Rules

- `.env` 절대 커밋 금지, 내용 출력 금지
- 키움 OpenAPI는 32-bit Python만 지원
- 매매 로직 수정 시 반드시 백테스트 실행
- 손실 방어 로직(MAX_DAILY_LOSS, 손절, 트레일링) 제거/완화 절대 금지
- 전략 파일 내 리스크 파라미터(K값, 손절폭, 슬롯 수 등)를 공격적으로 변경하는 요청은 위험성을 경고 후 진행
- 대충 작업 금지: Explore → Plan → 실행 → Verify → Backtest
- 아빠 작업 시 한 번에 하나의 단계만 안내
- _state.py 수정 시 모든 alerts/ 모듈에 영향 — 전체 임포트 검증 필수
