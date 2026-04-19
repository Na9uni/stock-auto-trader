# 아들 PC (son-dev) 리뷰 대응 작업 지시서

> **이 문서는 son-dev 브랜치의 Claude Code가 읽고 순서대로 실행합니다.**
> 사람이 읽는 일반 문서가 아니라 **자동 실행용 명령어 리스트**입니다.
> dad-dev(아빠 PC)가 PR #1 리뷰 후 남긴 Must/Should 지적 5건을 처리합니다.

---

## 0. 배경

이 지시서는 **son-dev 브랜치에만** 해당합니다.

PR #1(son-dev → master) 리뷰에서 지적된 5건 중 son-dev 소관을 처리합니다:

- **Must 2건** (머지 전 필수)
  1. `docs/ARCHITECTURE.md` 존재 확인/생성 — CLAUDE.md 74줄이 참조하는데 파일 누락 의심
  2. SessionStart 훅의 JSON 파싱 `try/except` 보강
- **Should 3건** (품질 개선, 가능하면)
  3. `judge-generator` 스킬에 실전 호출 예시 추가
  4. L4 CLAUDE.md 3개에 "80줄 이하" 원칙 명시
  5. SessionStart 여러 서브프로세스를 단일 스크립트로 통합 (#2와 통합 해결 가능)

dad-dev에서 이미 처리한 항목(무효 훅 교체)은 머지 후 자동 반영되므로 son-dev에서 추가 작업 불필요.

---

## 1. 실행 전 체크 (Claude 직접 확인)

```bash
# 현재 위치 (프로젝트 루트)
pwd

# 브랜치 확인 — 반드시 son-dev여야 함
git branch --show-current
```

**출력이 `son-dev`가 아니면 즉시 중단, 사용자에게 보고.**

```bash
# 원격 동기화
git fetch origin
git pull origin son-dev

# 상태
git status
```

modified/untracked 파일이 있으면 사용자에게 보고 후 결정 (임시 커밋 or stash).

---

## 2. 작업 #1 (Must): `docs/ARCHITECTURE.md` 존재 확인 + 생성

### 2-1. 존재 여부 확인

```bash
ls -la docs/ARCHITECTURE.md 2>&1
```

### 2-2. 파일 존재 + 내용 OK → 스킵

```bash
# 존재 시 핵심 섹션 확인
grep -E "Module Dependency Map|Strategy Hierarchy|Key Patterns" docs/ARCHITECTURE.md
```

3개 모두 매칭되면 **작업 #1 스킵, 작업 #2로**.

### 2-3. 파일 없음 또는 비어있음 → Write tool로 생성

**정확히 아래 내용**으로 `docs/ARCHITECTURE.md` 생성:

````markdown
# Stock Auto Trader — Architecture

> CLAUDE.md에서 분리된 상세 아키텍처 문서. 매 세션마다 자동 로드되지 않고, 필요 시 참조.

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
      "current_price": 0,
      "change_rate": 0.0,
      "prev_volume": 0,
      "candles_1m": [],
      "candles_1d": []
    }
  },
  "indices": {},
  "account": {}
}
```

### data/auto_positions.json
```json
{
  "<ticker>": {
    "qty": 0,
    "buy_price": 0,
    "buy_amount": 0,
    "high_price": 0,
    "trailing_activated": false,
    "rule_name": "",
    "intent": "daytrading|swing",
    "manual": false,
    "mock": false
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

## Process Architecture (2-Process)

- **32-bit Python (kiwoom/)**: 키움 OpenAPI+ (OCX는 32-bit만 지원)
- **64-bit Python (alerts/, strategies/, backtest/)**: 스케줄러/분석/전략
- **IPC**: `data/kiwoom_data.json` 파일 (atomic write)

## Related Documents

- 매매 설정: `.env`
- 전략 추가: `.claude/skills/add-strategy.md`
- 도메인 지식: `.claude/skills/domain-knowledge.md`
- 백테스트: `.claude/skills/backtest.md`
- 디렉토리별 규칙: `strategies/CLAUDE.md`, `alerts/CLAUDE.md`, `backtest/CLAUDE.md`
````

### 2-4. 생성 후 검증

```bash
wc -l docs/ARCHITECTURE.md
# 예상: 80~100줄

grep -c "Module Dependency Map\|Strategy Hierarchy\|Key Patterns" docs/ARCHITECTURE.md
# 출력: 3
```

---

## 3. 작업 #2 + #5 (Must + Should 통합): SessionStart 훅 재설계

### 3-1. 현재 상태 확인

```bash
grep -A 5 '"SessionStart"' .claude/settings.json | head -30
```

여러 `python -c "json.load(...)"` 명령이 연속 호출되는 구조 확인.

### 3-2. 문제 파악

- Python 서브프로세스 3~4개 순차 호출 (성능 저하)
- 각 `json.load`에 `try/except` 없음 (파일 손상 시 에러 출력)

### 3-3. 해결: 단일 Python 스크립트로 분리 + try/except 감싸기

**Step 1: `scripts/` 디렉토리 생성**

```bash
mkdir -p scripts
```

**Step 2: Write tool로 `scripts/session_start_check.py` 생성**

정확히 아래 내용:

```python
"""세션 시작 환경 점검 (SessionStart 훅에서 호출).

모든 JSON 파싱을 try/except로 감싸 손상된 파일에도 경고만 출력.
여러 서브프로세스를 단일 스크립트로 통합하여 성능 개선.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _safe_json_load(path: Path) -> dict | None:
    """JSON 안전 로드 — 실패 시 WARN 후 None 반환."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[WARN] {path.name} 읽기 실패: {type(exc).__name__}", file=sys.stderr)
        return None


def main() -> None:
    print("[세션 시작 환경 점검]", file=sys.stderr)

    # .env 존재 여부
    if not (ROOT / ".env").exists():
        print("⚠️  .env 파일 없음 — 키움 API/텔레그램 동작 불가", file=sys.stderr)

    # 매크로 override
    macro = _safe_json_load(ROOT / "data" / "macro_override.json")
    if macro is not None:
        print(
            f"매크로 override: war_active={macro.get('war_active')}, "
            f"vkospi={macro.get('vkospi')}",
            file=sys.stderr,
        )

    # 수집기 마지막 업데이트
    kd = _safe_json_load(ROOT / "data" / "kiwoom_data.json")
    if kd is not None:
        print(f"수집기 마지막 업데이트: {kd.get('updated_at', '')}", file=sys.stderr)

    # 레짐 상태
    regime = _safe_json_load(ROOT / "data" / "regime_state.json")
    if regime is not None:
        reason = str(regime.get("reason", ""))[:40]
        print(f"시스템 레짐: {regime.get('state')} (사유: {reason})", file=sys.stderr)

    # 브랜치
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=ROOT, text=True,
        ).strip()
        print(f"현재 브랜치: {branch}", file=sys.stderr)
        if branch == "master":
            print("⚠️  master 직접 작업 금지! son-dev로 전환 필요", file=sys.stderr)
    except Exception:
        pass


if __name__ == "__main__":
    main()
```

**Step 3: `.claude/settings.json`의 SessionStart 훅 `command` 값 교체**

Edit tool로 현재 SessionStart 블록의 긴 bash 명령을 아래 한 줄로 교체:

```json
"command": "python scripts/session_start_check.py"
```

**주의**: JSON 이스케이프 조심. Edit tool로 `command` 필드 값만 정확히 교체.

### 3-4. 검증

```bash
# JSON 유효성
python -c "import json; json.load(open('.claude/settings.json', encoding='utf-8'))" && echo "JSON OK"

# 스크립트 직접 실행
python scripts/session_start_check.py

# 예상 출력 (stderr):
# [세션 시작 환경 점검]
# (파일 있는 만큼 정보 / 없거나 손상되면 WARN)
# 현재 브랜치: son-dev
```

---

## 4. 작업 #3 (Should): `judge-generator` 스킬에 실전 호출 예시 추가

### 4-1. 현재 파일 확인

```bash
tail -30 .claude/skills/judge-generator.md
```

`## 연계 스킬` 섹션 찾기.

### 4-2. Edit tool로 `## 연계 스킬` 바로 앞에 신규 섹션 삽입

```markdown
## 실전 호출 예시

### VB K값 최적화 (단일 종목)
```bash
py -m backtest.k_optimizer --ticker 229200 --k_start 0.3 --k_end 1.0 --k_step 0.1
```

### Walk-Forward 자동 재최적화 (전 종목)
```bash
py -m backtest.walk_forward_auto
# 출력: 20종목별 K값 추천, 과적합 경고, train vs test Sharpe 비교
```

### 파라미터 그리드 서치 (VB 4개 변수 × 3종목)
```bash
py backtest/grid_search.py           # Full (144 조합)
py backtest/grid_search.py --small   # Small (36 조합)
py backtest/grid_search.py --sensitivity  # 민감도 분석 포함
```

### PD 루프 (Judge-Generator 수동 실행)

1. Bash 도구로 위 스크립트 실행
2. 결과 txt/csv 파싱 → PASS/FAIL 판정
3. FAIL이면 다음 파라미터 조합 결정
4. 1~3 반복 (MAX_ITER=5)
5. 통과 또는 MAX_ITER 도달 시 사용자 보고

## 연계 스킬
...
```

(기존 `## 연계 스킬` 내용은 유지)

---

## 5. 작업 #4 (Should): L4 CLAUDE.md 3개에 원칙 섹션 추가

### 5-1. 현재 라인 수

```bash
wc -l strategies/CLAUDE.md alerts/CLAUDE.md backtest/CLAUDE.md
```

### 5-2. 각 파일 맨 아래에 동일 섹션 Edit tool로 추가

3개 파일 모두 맨 아래(EOF 직전)에 아래 섹션 추가:

```markdown

## 이 파일의 원칙

- 80줄 이하 유지 (상위 `CLAUDE.md`와 동일 원칙)
- 디렉토리별 즉시 필요한 규칙만
- 상세 레퍼런스는 `.claude/skills/*` 참조
- 파일 목록/코드 스니펫 금지 (코드 직접 읽기)
```

### 5-3. 80줄 초과하면 기존 내용 중복 제거

```bash
wc -l strategies/CLAUDE.md alerts/CLAUDE.md backtest/CLAUDE.md
```

80줄 초과 시 기존 내용에서 중복/명백한 섹션 1~2줄 삭제.

---

## 6. 최종 검증

**모든 작업 완료 후 아래 명령 순차 실행. 하나라도 실패 시 중단 + 사용자 보고.**

```bash
echo "=== 1. 신규 파일 존재 ==="
ls docs/ARCHITECTURE.md scripts/session_start_check.py

echo "=== 2. JSON 유효 ==="
python -c "import json; json.load(open('.claude/settings.json', encoding='utf-8'))" && echo "JSON OK"

echo "=== 3. 세션 체크 스크립트 실행 ==="
python scripts/session_start_check.py

echo "=== 4. 모든 CLAUDE.md 80줄 이하 ==="
wc -l CLAUDE.md strategies/CLAUDE.md alerts/CLAUDE.md backtest/CLAUDE.md
# 모두 80줄 이하여야 함. 초과 시 수정 필요.

echo "=== 5. pytest 전체 통과 ==="
python -m pytest tests/ -q --tb=short 2>&1 | tail -3

echo "=== 6. 핵심 임포트 ==="
python -c "from strategies.combo_strategy import ComboStrategy; from strategies.auto_strategy import AutoStrategy; from config.trading_config import TradingConfig; from alerts.analysis_scheduler import run_scheduler" && echo "IMPORT OK"

echo "=== 7. git status ==="
git status --short
# 예상 변경:
#  M .claude/settings.json
#  M .claude/skills/judge-generator.md
#  M strategies/CLAUDE.md
#  M alerts/CLAUDE.md
#  M backtest/CLAUDE.md
# ?? docs/ARCHITECTURE.md
# ?? scripts/session_start_check.py
```

---

## 7. 커밋 & PR #1 업데이트

```bash
git add docs/ARCHITECTURE.md \
        scripts/session_start_check.py \
        .claude/settings.json \
        .claude/skills/judge-generator.md \
        strategies/CLAUDE.md \
        alerts/CLAUDE.md \
        backtest/CLAUDE.md

git commit -m "$(cat <<'EOF'
fix: PR #1 리뷰 지적 5건 대응 (docs/ARCHITECTURE + try/except + 예시 + 원칙)

dad-dev 리뷰에서 지적된 5건 중 son-dev 소관 처리.

## Must (머지 전 필수)
1. docs/ARCHITECTURE.md 신규 — CLAUDE.md 74줄 슬림본이 참조하나 파일 누락.
   Module Map / Strategy Hierarchy / Key Patterns / Data Schema / Regime /
   2-Process 아키텍처 포함.
2. SessionStart 훅 → scripts/session_start_check.py 분리 + try/except:
   - 기존: python -c 3~4개 (JSON 파싱 실패 시 에러 출력)
   - 신규: 단일 Python 스크립트 + 모든 JSON load에 try/except
   - 성능: subprocess 1개로 감소 (#5 함께 해결)
   - 안정성: 손상된 JSON 파일에도 WARN만 출력

## Should (품질 개선)
3. judge-generator 스킬에 실전 호출 예시 추가
   (k_optimizer / walk_forward_auto / grid_search)
4. L4 CLAUDE.md 3개(strategies/ alerts/ backtest/)에 '80줄 이하' 원칙 명시
5. SessionStart 서브프로세스 통합 (#2와 함께 해결)

## 검증
- pytest 전체 통과
- JSON 유효
- 핵심 임포트 성공
- 모든 CLAUDE.md 80줄 이하

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

git push origin son-dev
```

**PR #1 자동 업데이트됨.**

---

## 8. 문제 발생 시 대응

### Q: `docs/ARCHITECTURE.md`가 이미 있고 내용이 다르면?
→ 기존 내용 유지. **절대 덮어쓰지 말 것.** 사용자에게 "기존 파일 존재, 병합 필요?" 물어보고 지시 대기.

### Q: `.claude/settings.json` 편집 중 JSON 문법 깨짐?
→ 즉시 `git checkout .claude/settings.json` 롤백. 다시 시도.

### Q: pytest 실패?
→ 에러 메시지 읽고 수정. 3회 실패 시 중단 + 사용자 보고.
→ 네트워크 관련 실패(yfinance 등)는 배제 판단.

### Q: `scripts/session_start_check.py` ImportError?
→ Python 버전 확인 (3.11+ 필요). `from __future__ import annotations` 라인 확인.

### Q: L4 CLAUDE.md에 "원칙" 섹션 추가 후 80줄 초과?
→ 기존 내용 중 가장 짧고 중복되는 1~2줄 삭제. 새 섹션은 4~5줄로 최소화.

### Q: PR #1이 이미 머지된 상태라면?
→ son-dev는 별도 PR 생성:
```bash
gh pr create --base master --head son-dev \
  --title "fix: dad-dev 리뷰 지적 5건 대응 (docs/ARCHITECTURE + try/except)"
```

### Q: 예상치 못한 에러?
→ `git stash push -u -m "emergency-backup"` 으로 백업 후 사용자에게 보고. 임의 판단 금지.

---

## 9. 완료 보고 양식

모든 단계 성공 시 아래 형식으로 보고:

```
✅ PR #1 리뷰 대응 완료 (son-dev)

## 처리 항목
- [x] Must #1 docs/ARCHITECTURE.md 생성 (NN줄)
- [x] Must #2 SessionStart try/except + subprocess 통합
       → scripts/session_start_check.py (NN줄)
- [x] Should #3 judge-generator 호출 예시 추가
- [x] Should #4 L4 CLAUDE.md 3개 원칙 명시
- [x] Should #5 (Must #2와 통합 해결)

## 검증
- pytest: NN passed
- JSON 유효: OK
- CLAUDE.md 라인: 루트 NN / strategies NN / alerts NN / backtest NN
- 임포트 체인: OK

## 커밋
- SHA: <short sha>
- PR #1 자동 업데이트 완료
- URL: https://github.com/Na9uni/stock-auto-trader/pull/1

## 다음 단계
- 아빠 PC에 "PR #1 머지 가능" 알림
- 머지 후 아빠 PC: git checkout dad-dev && git merge origin/master
```

---

## 핵심 요약 (TL;DR for Claude)

1. `git branch --show-current` → `son-dev` 확인. 아니면 중단.
2. `git pull origin son-dev` 최신화.
3. **작업 #1**: `docs/ARCHITECTURE.md` 없으면 Write (본문 그대로).
4. **작업 #2+#5**: `scripts/session_start_check.py` Write + `.claude/settings.json` SessionStart 훅을 `python scripts/session_start_check.py` 한 줄로 교체.
5. **작업 #3**: `.claude/skills/judge-generator.md`의 `## 연계 스킬` 앞에 `## 실전 호출 예시` 섹션 Edit.
6. **작업 #4**: `strategies/CLAUDE.md`, `alerts/CLAUDE.md`, `backtest/CLAUDE.md` 맨 아래에 `## 이 파일의 원칙` 섹션 Edit.
7. 검증 7개 전부 통과.
8. 커밋 + push.
9. 완료 보고.

**순서대로. 실패 시 중단 + 보고. 임의 판단 금지.**
