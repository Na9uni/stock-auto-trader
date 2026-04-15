# Stock Auto Trader — v3 업그레이드 패키지

## 적용 방법

1. 기존 `CLAUDE.md`, `.claude/settings.json`, `.claude/skills/expert-meeting.md` 백업
2. 이 폴더의 내용을 프로젝트 루트(`stock/`)에 복사 (덮어쓰기)
3. 기존 skills 중 유지할 것: start-session, end-session, verify, backtest, add-strategy
4. 기존 skills 중 교체할 것: expert-meeting (Subagent Context 섹션 추가)

## 파일 목록

### 덮어쓰기 (기존 교체)
| 파일 | 변경 내용 |
|------|----------|
| `CLAUDE.md` | Domain Expertise를 스킬로 분리 → 95줄로 축소 |
| `.claude/settings.json` | Stop 훅 버그 수정, pytest 자동실행, .env 보호 강화 |
| `.claude/skills/expert-meeting.md` | Subagent Context + 출력 제한 추가 |

### 신규 추가
| 파일 | 용도 |
|------|------|
| `.claude/skills/quant-pd.md` | **퀀트PD 메인 페르소나** — 리스크 퍼스트 사고, 시장 먼저 코드 나중, 근거 기반 판단 |
| `.claude/skills/expert-solo.md` | **전문가 1인 개별 소환** — 전체 회의 없이 특정 관점 리뷰 |
| `.claude/skills/trading-review.md` | 도메인 기반 자동 코드 리뷰 (7개 체크리스트) |
| `.claude/skills/domain-knowledge.md` | 트레이딩 도메인 지식 레퍼런스 |
| `.claude/skills/debug-signal.md` | 신호 문제 디버깅 6단계 |
| `.claude/skills/impact-check.md` | 코드 변경 영향 범위 분석 |

## v2 대비 수정 사항

### 버그 수정 (v3)
- **Stop 훅**: `git diff --name-only` → `git diff --name-only HEAD` (staged 파일도 감지)
- **expert-meeting**: v2에서 누락된 Subagent Context 섹션 추가

### 기능 추가 (v3)
- **PostToolUse**: 전략 파일 변경 시 `pytest tests/test_strategies.py` 자동 실행
- **PreCommit**: 전략 파일 커밋 시 전체 `pytest tests/` 실행 (실패 시 커밋 차단)
- **domain-knowledge.md**: 변동 가능 수치에 "사용자 확인 필요" 표기

### 구조 개선 (v3)
- **CLAUDE.md**: 144줄 → 110줄 (Domain Expertise 상세 내용을 스킬로 분리)
- **expert-meeting.md**: 서브에이전트별 출력 제한(1000토큰) 명시

### 시나리오 기반 보강 (v3.1)
- **에러 복구**: pytest 실패 시 3회 재시도 후 사용자 보고
- **전략 리스크 파라미터 보호**: K값/손절폭 등 공격적 변경 시 경고
- **REJECT 프로토콜**: REJECT 시 롤백 → 사유 보고 → 대안 제시
- **스킬 로딩 제한**: 무거운 스킬 3개 동시 로드 금지
- **페르소나별 제안 톤**: 아들=1~2줄, 아빠=쉽게 풀어서
- **세션 핸드오프**: 세션 교체 시 요약 전달 가이드

## 알려진 한계

### .env 우회 가능성
`cat .env`는 deny로 차단되지만, `python -c "print(open('.env').read())"` 같은
간접 경로는 기술적으로 차단 불가 (python 실행 자체를 차단할 수 없으므로).
CLAUDE.md의 "내용 출력 금지" 규칙 + PreToolUse의 .env 수정 차단으로 보완.

### Auto Review는 advisory
CLAUDE.md의 Auto Review Protocol은 ~80% 준수율.
Stop 훅이 보완하지만, "리뷰를 실제로 수행했는지"는 검증 불가.
결정적 보장이 필요한 검증(문법, 테스트)은 hooks로, 도메인 리뷰는 advisory로 이원화.

### 비용 수치 검증 필요
domain-knowledge.md의 수수료/세금 수치는 변동 가능.
실제 적용 전 키움증권 현재 수수료율, 최신 증권거래세율 확인 필요.
