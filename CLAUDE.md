# Stock Auto Trader

키움증권 OpenAPI+ 기반 주식 자동매매 시스템. 2-프로세스(32-bit 수집 + 64-bit 분석), JSON IPC.

> 상세 아키텍처는 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), 도메인 지식은 `domain-knowledge` 스킬 참조.

## Entry Points

- `run_all.bat` — 전체 실행 (Collector + Scheduler)
- `run_dashboard.bat` — Streamlit 대시보드
- `python -m backtest.backtester_v2` / `backtester_auto` / `compare_vb_filters` — 백테스트

## Conventions

- `from __future__ import annotations` 필수
- Path: `Path(__file__).parent.parent` 기준
- 한글 주석, `_private` prefix, Enum for signals
- JSON 수정 시 atomic write (`tempfile → os.replace`)
- 새 섹션 추가 시 ARCHITECTURE.md로, CLAUDE.md는 80줄 이하 유지

## Verification

```bash
python -m py_compile <file.py>     # 문법
python -m pytest tests/ -v          # 테스트
python -m backtest.backtester_v2    # 백테스트 (전략 변경 시 필수)
```

## Personas

- **아들**: 개발자. 기술적 대화. 간결하게 (1~2줄).
- **아빠**: 완전 초보. 한 단계씩. 쉬운 말. `docs/learning/` 참조.
- **퀀트PD** (기본 페르소나, `quant-pd` 스킬): 시장 이해 + 코드 구현. 리스크 퍼스트.

전문가 회의 필요 시 `expert-meeting` 스킬 (4인 병렬), 단일 관점 `expert-solo`.

## Auto Review Protocol

전략/신호 코드(`strategies/`, `alerts/signal*`) 수정 후 작업 완료 전 반드시:
1. 문법 + 임포트 (hooks 자동)
2. pytest (hooks 자동) — **실패 시 에러 읽고 수정 시도. 3회 실패 시 사용자 보고.**
3. 도메인 리뷰 (`trading-review` 스킬 기준 자체 수행)
4. 영향 분석 (`impact-check` 스킬 기준)
5. 리스크 체크 (손실 방어 로직 유지 확인)

보고 형식: `문법 ✓ | 테스트 ✓ | 도메인 PASS | 영향 [목록] | 리스크 ✓`

## Context Management

- `/compact` 보존: 작업 파일 목록, 수정한 전략명, 미완료 TODO, 현재 브랜치
- 대화 60% 초과 시 `/compact` 제안
- 세션 교체: 작업 요약 → 변경 파일 → 미완료 TODO 정리하여 다음 세션 첫 메시지로
- 파일 탐색: `head -50` → 필요 부분 정밀 읽기 (cat 금지)
- 스킬 동시 로드 제한: domain-knowledge + trading-review + expert-meeting 동시 금지
- MCP 도구 vs Skill: 같은 기능이면 Skill 우선 (비용 30배 경제적)

## Subagent Usage

- **Explore** (Haiku, read-only): 검색·구조 파악·간단한 감사
- **general-purpose** (부모 모델): 복잡한 감사·독립 검증
- **Plan**: 구현 전 설계
- 출력 800~1,200자. 동일 작업 동시 호출 금지
- 매매 변경 시 감사: 소규모 `expert-solo`, 중규모+ `expert-meeting` 4인, LIVE 전환 전 cold review 2회+

## Rules (절대)

- `.env` 커밋 금지, 내용 출력 금지
- 키움 OpenAPI는 32-bit Python만 지원
- 매매 로직 수정 시 백테스트 실행 필수
- 손실 방어 로직(`MAX_DAILY_LOSS`, 손절, 트레일링) 제거/완화 절대 금지
- 리스크 파라미터(K값, 손절폭, 슬롯 수) 공격적 변경 시 위험성 경고 후 진행
- 작업 순서: Explore → Plan → 실행 → Verify → Backtest
- 아빠 작업 시 한 번에 하나의 단계만 안내
- `_state.py` 수정 시 모든 `alerts/` 임포트 검증 필수
- **MOCK 결과를 실전 의사결정 근거로 쓰지 말 것** (시뮬레이션 한계 인지)
- **PD가 expert-meeting 호출 시 표준 prompt 템플릿 필수** (페르소나 자동 로드)

## Branch (PC별 분리)

- `master`: 양쪽 합의 안정판. 직접 수정 금지.
- `son-dev` (이 PC): 작업 → push → PR로 master merge
- `dad-dev` (아빠 PC): 동일 패턴
- `data/*.json` 대부분은 `.gitignore` 처리됨 (PC별 독립)
