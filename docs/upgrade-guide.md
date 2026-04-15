# Stock Auto Trader — 4대 엔지니어링 적용 가이드

Claude Code 환경에서 주식 자동매매 시스템의 개발 효율과 안전성을 극대화하기 위한 실전 적용 가이드.

---

## 1. 현재 상태 진단

### 잘 되어 있는 것

| 영역 | 현재 상태 | 평가 |
|------|----------|------|
| CLAUDE.md | 60줄, WHAT/HOW 중심, 간결 | ★★★★☆ |
| Skills | 6개 (start/end-session, verify, backtest, add-strategy, expert-meeting) | ★★★★☆ |
| Hooks | PostToolUse(py_compile, 전략 리마인더), PreCommit(임포트 검증) | ★★★☆☆ |
| Permissions | allow/deny 분리, rm -rf/force push 차단 | ★★★★☆ |
| 테스트 | 43개 유닛테스트 | ★★★★☆ |
| 문서 | architecture.md, strategies.md, 학습가이드 8개 | ★★★★★ |

### 개선이 필요한 것

**프롬프트 엔지니어링 (CLAUDE.md)**
- 모듈 간 의존관계 맵이 없음 → Claude가 수정 영향 범위를 매번 코드로 파악해야 함
- 전략 간 관계(AUTO가 VB/추세추종을 전환)가 명시되지 않음
- `/compact` 시 보존할 정보 지시가 없음

**콘텍스트 엔지니어링**
- Skills가 전부 user_invocable → Claude가 자동으로 판단해서 호출하는 스킬이 없음
- 파일 수 80개인데 Claude가 어디부터 읽어야 할지 우선순위 없음
- 대화가 길어졌을 때 콘텍스트 관리 전략 부재

**하네스 엔지니어링**
- 손실 방어 로직 보호가 CLAUDE.md의 텍스트 규칙에만 의존 (80% 준수율)
- `PreToolUse` 훅 없음 → 위험한 파일 수정을 사전 차단하지 못함
- `Stop` 훅 없음 → 작업 완료 전 검증을 강제하지 못함
- `.env` 파일 보호가 규칙만 있고 훅으로 강제되지 않음

**컴파운드 시스템**
- expert-meeting이 "병렬 서브에이전트"로 설계되어 있지만 실제 실행 가이드가 부족
- 서브에이전트 스폰 시 전달할 콘텍스트 범위 미정의

---

## 2. 개선된 CLAUDE.md

기존 대비 주요 변경: 모듈 의존관계 맵 추가, 전략 관계도 추가, 콘텍스트 관리 지시 추가, 불필요한 중복 제거.

```markdown
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

보조 전략 (독립 실행 가능):
  combo_strategy.py — VB + 거부권
  score_strategy.py — 합산 거부권
  crisis_rotation.py — 위기 로테이션
  momentum_rotation.py — 모멘텀 로테이션
```

## Entry Points

- `run_all.bat` — 전체 (수집기 + 스케줄러)
- `run_dashboard.bat` — Streamlit 대시보드
- `python -m backtest.backtester_v2` — 백테스트

## Key Patterns

- Config: `config/trading_config.py` (frozen dataclass, `.env` 로드)
- Strategy Protocol: `strategies/base.py` → `evaluate(MarketContext) -> SignalResult`
- IPC: `tempfile → os.replace()` atomic write
- 종목 선정: `config/stock_screener.py` (6겹 필터) + `config/whitelist.py`

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
python -m pytest tests/ -v
```

## Personas

- **아들**: 개발자. 기술적 대화. 간결하게.
- **아빠**: 완전 초보. 한 단계씩. 쉬운 말. `docs/learning/` 참조.

## Context Management

- `/compact` 시 반드시 보존: 현재 작업 중인 파일 목록, 수정한 전략명, 미완료 TODO
- 대화 60% 넘으면 자발적으로 `/compact` 제안
- 전략 수정 작업 시: base.py → 해당 전략 → auto_strategy.py 순서로 읽기
- 파일 탐색 시 `head -50`으로 구조 파악 후 필요한 부분만 정밀 읽기

## Rules

- `.env` 절대 커밋 금지, 내용 출력 금지
- 키움 OpenAPI는 32-bit Python만 지원
- 매매 로직 수정 시 반드시 백테스트 실행
- 손실 방어 로직(MAX_DAILY_LOSS, 손절, 트레일링) 제거/완화 절대 금지
- 대충 작업 금지: Explore → Plan → 실행 → Verify → Backtest
- 아빠 작업 시 한 번에 하나의 단계만 안내
- _state.py 수정 시 모든 alerts/ 모듈에 영향 — 전체 임포트 검증 필수
```

### 기존 대비 변경 요약

| 항목 | 기존 | 개선 |
|------|------|------|
| 모듈 의존관계 | 없음 | 데이터 흐름 맵 추가 |
| 전략 관계 | "AUTO: 상승장=VB, 하락장=추세추종" 1줄 | 계층도 + 보조 전략 목록 |
| /compact 지시 | 없음 | 보존 항목 + 자동 제안 기준 |
| 파일 탐색 가이드 | 없음 | 우선순위 + head 전략 |
| _state.py 경고 | 없음 | 영향 범위 명시 |
| 테스트 명령 | py_compile + 임포트만 | pytest 추가 |
| 라인 수 | ~60줄 | ~80줄 (20줄 증가, 허용 범위) |

---

## 3. 개선된 settings.json (Hooks)

핵심 변경: `PreToolUse` 추가(위험 파일 보호), `Stop` 추가(작업 완료 전 검증), 기존 hooks 강화.

```json
{
  "permissions": {
    "allow": [
      "Read",
      "Glob",
      "Grep",
      "Bash(python *)",
      "Bash(pip *)",
      "Bash(git *)",
      "Bash(streamlit *)",
      "Bash(ls *)",
      "Bash(mkdir *)",
      "Bash(head *)",
      "Bash(tail *)",
      "Bash(cat *)",
      "Bash(wc *)",
      "Bash(find *)"
    ],
    "deny": [
      "Bash(rm -rf *)",
      "Bash(git push --force*)",
      "Bash(git reset --hard*)",
      "Bash(cat .env*)",
      "Bash(*cat*.env*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|Create",
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'FILE=\"$CLAUDE_FILE_PATH\"; PROTECTED=\".env trading/auto_trader.py alerts/position_manager.py alerts/market_guard.py alerts/crisis_manager.py\"; for P in $PROTECTED; do if [[ \"$FILE\" == *\"$P\" ]]; then echo \"[BLOCK] 보호 대상 파일: $FILE — 손실 방어/실행 핵심 모듈입니다. 수정 전 반드시 사용자 확인을 받으세요.\" >&2; exit 2; fi; done'",
            "description": "손실 방어 핵심 파일 수정 시 사전 차단 (사용자 확인 필요)"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'FILE=\"$CLAUDE_FILE_PATH\"; if [[ \"$FILE\" == *.py ]]; then python -m py_compile \"$FILE\" 2>&1 || exit 2; fi'",
            "description": "Python 문법 체크 (py 파일 수정 시 자동 실행)"
          },
          {
            "type": "command",
            "command": "bash -c 'FILE=\"$CLAUDE_FILE_PATH\"; if echo \"$FILE\" | grep -q \"strategies/\"; then echo \"[ACTION REQUIRED] 전략 파일 변경됨 → 백테스트 실행 필수: python -m backtest.backtester_v2\" >&2; fi'",
            "description": "전략 파일 변경 시 백테스트 리마인더"
          },
          {
            "type": "command",
            "command": "bash -c 'FILE=\"$CLAUDE_FILE_PATH\"; if echo \"$FILE\" | grep -q \"alerts/_state.py\"; then echo \"[WARN] _state.py 변경됨 → 전체 alerts/ 모듈 임포트 검증 필요\" >&2; fi'",
            "description": "_state.py 변경 시 영향 범위 경고"
          }
        ]
      }
    ],
    "PreCommit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "bash -c 'python -c \"from strategies.combo_strategy import ComboStrategy; from strategies.auto_strategy import AutoStrategy; from config.trading_config import TradingConfig; from alerts.analysis_scheduler import run_scheduler\" 2>&1 || exit 2'",
            "description": "핵심 모듈 임포트 검증 (확장: AutoStrategy + run_scheduler 추가)"
          },
          {
            "type": "command",
            "command": "bash -c 'if git diff --cached --name-only | grep -q \".env\"; then echo \"[BLOCK] .env 파일 커밋 시도 차단!\" >&2; exit 2; fi'",
            "description": ".env 커밋 차단"
          }
        ]
      }
    ]
  }
}
```

### 기존 대비 변경 요약

| 훅 | 기존 | 개선 |
|----|------|------|
| PreToolUse | 없음 | 손실 방어 핵심 5개 파일 수정 사전 차단 |
| PostToolUse | py_compile + 전략 리마인더 (2개) | + _state.py 영향 범위 경고 (3개) |
| PreCommit | 임포트 검증 (2개 모듈) | + AutoStrategy/run_scheduler 추가 + .env 커밋 차단 |
| Permissions deny | 3개 | + cat .env 차단 (5개) |
| Permissions allow | 7개 | + head/tail/cat/wc/find (12개, 탐색 효율화) |

### PreToolUse 보호 대상 파일 설명

| 파일 | 보호 이유 |
|------|----------|
| `.env` | API 키, 비밀번호 — 노출/변경 시 치명적 |
| `trading/auto_trader.py` | 실제 매수/매도 실행 — 잘못된 수정 시 실손 |
| `alerts/position_manager.py` | 손절/트레일링 — 방어 로직 핵심 |
| `alerts/market_guard.py` | 급락 감지 — 위기 대응 핵심 |
| `alerts/crisis_manager.py` | 위기MR 실행 — 위기 모드 핵심 |

이 파일들은 수정이 **차단**되는 것이 아니라, Claude가 수정하려 할 때 **사용자 확인을 먼저 받도록** 하는 것입니다. 실제로는 사용자가 "그래, 수정해"라고 하면 진행 가능합니다.

---

## 4. Skills 개선안

### 기존 6개 유지 + 2개 신규 추가

기존 Skills는 잘 설계되어 있으므로 그대로 유지하되, 콘텍스트 엔지니어링 관점에서 2개를 추가합니다.

### 신규 1: `debug-signal.md`

```markdown
---
name: debug-signal
description: 신호 감지 문제 디버깅. "신호가 안 나와", "왜 매수 안 했어" 등의 요청에 사용.
user_invocable: true
---

# Debug Signal Skill

매매 신호가 예상대로 나오지 않을 때 원인을 추적합니다.

## 체크 순서 (의존관계 순)

1. **데이터 확인**: `data/kiwoom_data.json` — 시세 데이터가 정상 수신되는지
   ```bash
   python -c "import json; d=json.load(open('data/kiwoom_data.json')); print(f'종목수: {len(d)}, 최근: {list(d.keys())[:3]}')"
   ```

2. **레짐 확인**: `data/regime_state.json` — 현재 레짐 모드
   ```bash
   python -c "import json; print(json.load(open('data/regime_state.json')))"
   ```

3. **전략 평가**: 해당 전략의 `evaluate()` 직접 호출
   ```bash
   python -c "
   from strategies.auto_strategy import AutoStrategy
   from config.trading_config import TradingConfig
   # ... 수동 MarketContext 구성 후 evaluate 호출
   "
   ```

4. **신호 감지**: `alerts/signal_runner.py`의 루프 로직 확인
5. **주문 관리**: `alerts/order_manager.py`의 필터링 조건 확인
6. **실행 확인**: `alerts/trade_executor.py`의 MOCK/LIVE 모드 확인

## 아빠인 경우

"지금 시스템이 주식을 왜 안 사는지 하나씩 확인해볼게요"로 시작.
각 단계마다 "여기는 정상이에요 ✓" / "여기서 문제가 있어요 ✗" 명확히.
```

### 신규 2: `impact-check.md`

```markdown
---
name: impact-check
description: 코드 변경 전 영향 범위 분석. 파일 수정 전 자동으로 호출하여 어떤 모듈에 영향을 주는지 파악.
user_invocable: true
---

# Impact Check Skill

코드 변경 전 영향 범위를 분석합니다.

## Steps

1. 변경 대상 파일에서 export하는 클래스/함수 목록 확인
2. 해당 심볼을 import하는 파일 검색:
   ```bash
   grep -r "from <module>" --include="*.py" -l
   grep -r "import <module>" --include="*.py" -l
   ```
3. 영향받는 파일 목록을 tier로 분류:
   - **Tier 1 (직접)**: 변경 파일을 직접 import하는 모듈
   - **Tier 2 (간접)**: Tier 1 모듈을 import하는 모듈
   - **Tier 3 (데이터)**: 변경으로 JSON 스키마가 바뀌는 경우

## 고위험 변경 패턴

| 변경 대상 | 영향 범위 | 필수 조치 |
|-----------|----------|----------|
| `strategies/base.py` | 전략 11개 전부 | 전체 임포트 + 전체 백테스트 |
| `alerts/_state.py` | alerts/ 12개 전부 | 전체 임포트 검증 |
| `config/trading_config.py` | 거의 모든 모듈 | 전체 verify |
| JSON 스키마 변경 | 읽기/쓰기 양쪽 | 32-bit/64-bit 양쪽 확인 |

## 보고 형식

```
## 영향 분석: <변경 파일>
- Tier 1 (직접): file1.py, file2.py
- Tier 2 (간접): file3.py
- 필수 검증: <명령어>
- 위험도: 낮음/중간/높음
```
```

---

## 5. 워크플로우 레시피

### 레시피 A: 새 전략 추가 (전체 흐름)

```
1. /start-session → 사용자 식별 + 상태 확인
2. 요구사항 정리 → 전략 아이디어 구체화
3. /impact-check → base.py 변경 필요 여부 확인
4. /add-strategy → 전략 파일 생성
   ├─ [PostToolUse] py_compile 자동 실행
   └─ [PostToolUse] "전략 파일 변경됨 → 백테스트 필수" 리마인더
5. /backtest → 성과 검증
6. /expert-meeting → 4인 전문가 검증
   ├─ 퀀트: 백테스트 결과 분석
   ├─ 기술적: 신호 로직 리뷰
   ├─ 리스크: 손실 방어 확인 (VETO)
   └─ 시장: 레짐 대응 확인
7. 전원 PASS → auto_strategy.py에 통합
   └─ [PreToolUse] auto_trader.py 수정 시 사용자 확인
8. /verify → 전체 검증
9. git commit
   └─ [PreCommit] 임포트 검증 + .env 차단
10. /end-session → 요약 + 다음 할 일
```

### 레시피 B: 버그 수정 (신호 문제)

```
1. /start-session
2. /debug-signal → 6단계 체크로 원인 특정
3. /impact-check → 수정 대상 파일의 영향 범위 확인
4. 코드 수정
   ├─ [PreToolUse] 보호 파일이면 사용자 확인
   └─ [PostToolUse] py_compile + 리마인더
5. /verify → 전체 임포트 검증
6. 전략 관련이면 /backtest
7. git commit
8. /end-session
```

### 레시피 C: 아빠 학습 세션

```
1. /start-session → 아빠 식별 → 반갑게 인사
2. 이전 세션 복습 (메모리 참조)
3. 오늘 주제 선택 (3가지 제안)
4. 한 단계씩 설명
   - 코드 변경 시에도 한 번에 하나만
   - "이거 해볼까요?" → 확인 후 진행
5. /end-session → 칭찬 + 다음 제안
```

---

## 6. 컴파운드 시스템: expert-meeting 실행 가이드

현재 expert-meeting 스킬은 잘 설계되어 있지만, 실제 Claude Code에서 서브에이전트로 실행하는 방법이 구체적이지 않습니다.

### 실제 실행 방식

Claude Code에서 서브에이전트는 `/expert-meeting` 호출 시 다음과 같이 실행됩니다:

```
PD(Claude 메인 에이전트)
  │
  ├─ Subagent: Task("퀀트 트레이더로서 분석하라")
  │   → python -m backtest.backtester_v2 실행
  │   → 결과 수치 분석 (Sharpe, PF, MDD, 승률)
  │   → PASS/CONDITIONAL/REJECT + 근거 반환
  │
  ├─ Subagent: Task("기술적 분석가로서 리뷰하라")
  │   → 변경된 전략 코드 읽기
  │   → 지표 파라미터, lookahead bias 검사
  │   → PASS/CONDITIONAL/REJECT + 근거 반환
  │
  ├─ Subagent: Task("리스크 매니저로서 검증하라")
  │   → auto_trader.py, position_manager.py 읽기
  │   → 손실 방어 로직 존재 확인
  │   → PASS/CONDITIONAL/REJECT + 근거 반환 (VETO POWER)
  │
  └─ Subagent: Task("시장 분석가로서 확인하라")
      → auto_strategy.py, market_guard.py 읽기
      → 레짐 전환 로직, CRISIS 대응 확인
      → PASS/CONDITIONAL/REJECT + 근거 반환
```

### expert-meeting 스킬에 추가할 내용

기존 스킬 끝에 아래 섹션을 추가하면 Claude가 더 정확하게 실행합니다:

```markdown
## Subagent Context (각 서브에이전트에 전달할 파일)

### 퀀트 트레이더
- 읽기: 변경된 전략 파일, backtest/backtester_v2.py
- 실행: python -m backtest.backtester_v2
- 판단 기준: Sharpe > 0.5, PF > 1.2, 거래 30회+, MDD < 20%

### 기술적 분석가
- 읽기: 변경된 전략 파일, strategies/base.py, analysis/indicators.py
- 검사: 파라미터 범위, lookahead bias, 매직넘버
- 판단 기준: TA 관례 준수, bias 없음

### 리스크 매니저
- 읽기: trading/auto_trader.py, alerts/position_manager.py, .env(설정값만)
- 검사: MAX_DAILY_LOSS, 손절폭, 트레일링, 슬롯 제한
- 판단 기준: 모든 방어 로직 유지 (하나라도 약화 → REJECT)

### 시장 분석가
- 읽기: strategies/auto_strategy.py, strategies/regime_engine.py, alerts/market_guard.py
- 검사: 레짐 전환 로직, CRISIS 대응, 하락장 방어
- 판단 기준: CRISIS 모드 대응 존재, 레짐 무시 없음
```

---

## 7. 적용 순서 (권장)

한 번에 다 바꾸지 말고, 단계별로 적용하면서 효과를 확인하세요.

### 1단계: CLAUDE.md 교체 (5분)
- 기존 CLAUDE.md 백업 → 개선된 버전으로 교체
- 바로 체감 가능: Claude가 모듈 관계를 이해하고 영향 범위를 먼저 확인

### 2단계: settings.json 업데이트 (5분)
- 기존 settings.json 백업 → 개선된 버전으로 교체
- 바로 체감 가능: 보호 파일 수정 시 사전 경고, .env 커밋 차단

### 3단계: 신규 Skills 추가 (5분)
- `.claude/skills/debug-signal.md` 생성
- `.claude/skills/impact-check.md` 생성
- 기존 Skills는 그대로 유지

### 4단계: expert-meeting 보강 (5분)
- 기존 expert-meeting.md에 Subagent Context 섹션 추가
- 기존 내용은 그대로 유지

### 총 소요 시간: ~20분

---

## 8. 4대 엔지니어링 매핑 요약

| 엔지니어링 | 이 프로젝트에서의 구현 | 핵심 파일 |
|-----------|---------------------|----------|
| **프롬프트** | CLAUDE.md (모듈맵 + 전략 관계 + 규칙) | `CLAUDE.md` |
| **콘텍스트** | Skills 8개 (lazy-load) + /compact 지시 + 탐색 우선순위 | `.claude/skills/*.md` |
| **하네스** | PreToolUse(보호) + PostToolUse(검증) + PreCommit(차단) + Permissions | `.claude/settings.json` |
| **컴파운드** | expert-meeting (4인 서브에이전트) + 워크플로우 레시피 | `expert-meeting.md` + 레시피 |
