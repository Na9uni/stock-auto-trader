# 아들 PC (son-dev) PR #1 충돌 해결 + 머지 지시서

> **이 문서는 son-dev 브랜치의 Claude Code가 읽고 순서대로 실행합니다.**
> PR #1을 머지 가능 상태로 만들기 위한 충돌 해결 + 검증 + 머지 절차.

---

## 0. 배경

PR #1 (`son-dev → master`) 상태:
- **mergeable: CONFLICTING** (DIRTY)
- master에 son-dev 분기 이후 **8개 커밋**이 추가됨 (자가 진단, 자본 효율, AI 수정 등 master 직접 푸시 누적분)
- son-dev는 이 변경을 받지 않은 상태 → master 머지 시 6파일 충돌

**선택한 해결 방식**: **옵션 A** — son-dev에서 master를 merge하여 충돌 해결 후 push.

**충돌 파일 6개** (대부분 다른 영역 수정 → 자동 병합 + 일부 수동):
- `alerts/trade_executor.py`
- `alerts/signal_runner.py`
- `alerts/position_manager.py` ⚠️ **보호 파일**
- `alerts/telegram_commander.py`
- `kiwoom/kiwoom_collector.py`
- `ai/ai_analyzer.py`

**머지 베이스**: `79606fb6` (양쪽이 마지막으로 같았던 커밋)

---

## 1. 사전 체크

```bash
pwd
# 프로젝트 루트인지 확인

git branch --show-current
# 출력: son-dev (반드시)
```

**브랜치가 son-dev 아니면 즉시 중단.**

```bash
# 현재 작업 상태
git status
# 깨끗해야 함. modified/staged 있으면 임시 커밋 또는 stash:
#   git stash push -u -m "before-merge-master"

# 원격 동기화
git fetch origin
git pull origin son-dev
```

---

## 2. master fetch + merge 시도

```bash
# master 최신화 확인
git log --oneline origin/son-dev..origin/master | head -10
# 8개 커밋 보일 것 (7cc0e86 등)

# merge 시작
git merge origin/master

# 예상 출력:
# Auto-merging alerts/market_guard.py
# Auto-merging config/trading_config.py
# Auto-merging alerts/signal_runner.py
# CONFLICT (content): Merge conflict in alerts/signal_runner.py
# Auto-merging alerts/trade_executor.py
# CONFLICT (content): Merge conflict in alerts/trade_executor.py
# (총 6개 CONFLICT)
# Automatic merge failed; fix conflicts and then commit the result.
```

```bash
# 충돌 파일 목록 확인
git diff --name-only --diff-filter=U
```

---

## 3. 충돌 해결 (파일별 가이드)

### 🔧 공통 원칙

각 파일에서 충돌 마커 `<<<<<<< HEAD` ~ `=======` ~ `>>>>>>> origin/master` 발견 시:
- `HEAD` 쪽 = son-dev 변경
- `origin/master` 쪽 = master 변경
- **양쪽 의도 모두 보존**이 원칙. 한쪽 버리지 말 것.
- 수정 후 마커 제거 + 의미 합치기

### 📋 파일별 의도 결합 가이드

#### 3-1. `alerts/trade_executor.py` (master 5 hunk + son-dev 6 hunk, 다른 영역)

**master 의도**:
- 자본 효율 극대화 — 매매금액 50만 고정
- 분할매수 비활성화

**son-dev 의도**:
- VETO 방어선 5개 복구
- max_slots 레짐 제약 복원: `min(MAX_SLOTS, regime.max_slots)`
- `is_watched()` / `is_whitelisted()` 분리 사용
- `MAX_ORDER_AMOUNT` 상한 복구

**병합 패턴**:
```
✅ master의 50만 고정 + 분할매수 비활성 적용
✅ son-dev의 VETO 방어선 5개 모두 유지
✅ master의 매매금액 로직 안에서 son-dev의 max_slots 제약 적용
✅ is_whitelisted 호출은 son-dev 그대로
```

대부분 다른 함수/영역이라 자동 병합 OK. 충돌 청크는 거의 없을 것.

#### 3-2. `alerts/signal_runner.py` (master 5 hunk + son-dev 9 hunk, 1 hunk 겹침)

**master 의도**:
- heartbeat 자가진단 9개 항목 포함
- `check_eod_liquidation` intent 기반 판정

**son-dev 의도**:
- MOCK CASH/DEFENSE 청산 복구 (이전 MOCK 스킵 → 양쪽 모두 청산)
- mock 파라미터 전달

**병합 패턴**:
```
✅ master의 heartbeat 자가진단 유지 (체크 항목 9개)
✅ son-dev의 MOCK CASH/DEFENSE 청산 로직 유지
✅ check_eod_liquidation은 master의 intent 기반 + son-dev의 mock 파라미터 둘 다
```

#### 3-3. `alerts/position_manager.py` ⚠️ 보호 파일 (master 6 hunk + son-dev 2 hunk)

**⚠️ 수정 전 사용자 확인 필수.**

**master 의도**:
- 분할매수 비활성 관련
- 자가진단 보조 로직

**son-dev 의도**:
- mock 파라미터 추가 (모든 손실 기록 함수에)

**병합 패턴**:
```
✅ master의 분할매수 비활성/자가진단 관련 유지
✅ son-dev의 mock 파라미터 모든 호출에 추가 유지
✅ 두 변경은 다른 함수에 있을 가능성 높음 → 자동 병합 시도
```

**보호 파일이므로 수정 후 사용자에게 변경 요약 보고**:
```
"[보호 파일 수정] alerts/position_manager.py merge 결과:
 - master 변경 (X줄): ...
 - son-dev 변경 (Y줄): ...
 - 충돌 영역: 없음/N줄
 진행 OK?"
```

#### 3-4. `alerts/telegram_commander.py` (master 3 hunk + son-dev 1 hunk, 1 hunk 겹침)

**master 의도**:
- 자가진단 메시지 추가
- 텔레그램 알림 형식 개선

**son-dev 의도**:
- `TELEGRAM_COMMANDER_ENABLED` 환경변수 스위치 (PC별 설정)

**병합 패턴**:
```
✅ master의 자가진단 메시지 유지
✅ son-dev의 ENABLED 스위치 유지 (None/false 시 polling 안 함)
```

#### 3-5. `kiwoom/kiwoom_collector.py` (master 2 hunk + son-dev 8 hunk, 1 hunk 겹침)

**master 의도**:
- AI 거래 부재 오판 근본 수정
- 체결강도 0일 때 AI 분석 스킵

**son-dev 의도**:
- prev_volume=0 일봉에서 보정
- 첫 틱 즉시 일봉/계좌/관심종목 수집
- Primary 15종목 배치 로테이션
- Daily 30종목 배치 로테이션
- SetRealReg 화면번호 분할

**병합 패턴**:
```
✅ master의 AI 오판 수정 + 체결강도 0 스킵 유지
✅ son-dev의 배치 로테이션 + prev_volume 보정 유지
✅ 두 변경은 거의 다른 영역 (AI 분석 호출 vs 데이터 수집)
✅ 1 hunk 겹침 → 수동 검토 후 양쪽 의도 모두 반영
```

#### 3-6. `ai/ai_analyzer.py` (master 3 hunk + son-dev 1 hunk, 다른 영역)

**master 의도**:
- AI 분석 메시지 쉬운 말로 변환 (아빠 요청)
- 데이터 문제 시 간단 메시지

**son-dev 의도**:
- AI 프롬프트 초보자 친화적으로 재작성

**병합 패턴**:
```
✅ master의 출력 메시지 변환 로직 유지
✅ son-dev의 입력 프롬프트 재작성 유지
✅ 둘 다 "아빠 친화"라는 같은 방향 → 충돌 적음
```

---

## 4. 충돌 해결 후 검증

```bash
# 1. 모든 충돌 마커 제거 확인
grep -rn "<<<<<<<\|=======\|>>>>>>>" alerts/ kiwoom/ ai/ config/ 2>&1
# 아무것도 출력되지 않아야 함. 출력되면 해당 파일 재수정.

# 2. 모든 파일 staged
git diff --name-only --diff-filter=U
# 출력 없어야 함 (모든 충돌 해결됨)

# 3. Python 문법
python -m py_compile alerts/trade_executor.py alerts/signal_runner.py \
                     alerts/position_manager.py alerts/telegram_commander.py \
                     kiwoom/kiwoom_collector.py ai/ai_analyzer.py \
  && echo "SYNTAX OK"

# 4. 핵심 임포트
python -c "
from strategies.combo_strategy import ComboStrategy
from strategies.auto_strategy import AutoStrategy
from config.trading_config import TradingConfig
from alerts.analysis_scheduler import run_scheduler
from alerts import trade_executor, market_guard, position_manager
from kiwoom.kiwoom_collector import KiwoomCollector
" && echo "IMPORT OK"

# 5. pytest (43 + master 신규 테스트 포함)
python -m pytest tests/ -q --tb=short 2>&1 | tail -5
# 모두 통과해야 함

# 6. SessionStart 스크립트 (PR #1 리뷰 대응에서 추가한 것)
python scripts/session_start_check.py
# 정상 출력 확인

# 7. JSON 유효
python -c "import json; json.load(open('.claude/settings.json', encoding='utf-8'))" && echo "JSON OK"
```

**하나라도 실패 시 중단 + 사용자 보고. 임의 패치 금지.**

---

## 5. 머지 커밋 + push

```bash
# 충돌 해결한 파일들 staging (자동 병합된 것 포함)
git add alerts/trade_executor.py \
        alerts/signal_runner.py \
        alerts/position_manager.py \
        alerts/telegram_commander.py \
        kiwoom/kiwoom_collector.py \
        ai/ai_analyzer.py \
        alerts/market_guard.py \
        config/trading_config.py

# 다른 자동 병합 파일도 확인
git status --short
# 모두 staging 됐는지 확인

# merge commit
git commit -m "$(cat <<'EOF'
merge: master into son-dev — PR #1 머지 준비 (6파일 충돌 해결)

master에 son-dev 분기 이후 8커밋 누적되어 PR #1 mergeable=CONFLICTING.
이 커밋으로 master를 son-dev에 merge하여 충돌 해결 + 머지 가능 상태로 전환.

## master 8커밋 (이번에 son-dev로 통합)
- 7cc0e86 27건 대규모 개선 + PC별 데이터 추적 제외
- e44af6d 자가 진단 항목별 정상/이상 텔레그램 알림
- 2d1e0cd 시작 시 자가 진단 시스템
- 40c139a 자본 효율 극대화 — 매매금액 50만 + 분할매수 비활성
- b500b48 AI 거래 부재 오판 근본 수정
- a6ffbc5 체결강도 0일 때 AI 분석 완전 스킵
- 20030ba AI 데이터 문제 시 간단 메시지
- 5c89fa5 AI 분석 메시지 쉬운 말로 변환

## 충돌 해결 원칙 (양쪽 의도 모두 보존)

### alerts/trade_executor.py
- master: 50만 고정 + 분할매수 비활성
- son-dev: VETO 방어선 5개 복구 (max_slots / is_whitelisted / MAX_ORDER_AMOUNT)
- 결과: 50만 고정 안에서 VETO 방어선 모두 적용

### alerts/signal_runner.py
- master: heartbeat 자가진단 9개
- son-dev: MOCK CASH/DEFENSE 청산 + mock 파라미터
- 결과: 자가진단 유지 + MOCK 청산 로직 추가

### alerts/position_manager.py (보호 파일)
- master: 분할매수 비활성 관련
- son-dev: mock 파라미터 추가
- 결과: 두 변경 다른 함수 → 양쪽 보존

### alerts/telegram_commander.py
- master: 자가진단 메시지
- son-dev: TELEGRAM_COMMANDER_ENABLED 스위치
- 결과: 메시지 유지 + ENABLED 가드 적용

### kiwoom/kiwoom_collector.py
- master: AI 오판 수정 + 체결강도 0 스킵
- son-dev: prev_volume 보정 + 배치 로테이션 + SetRealReg 분할
- 결과: 양쪽 다른 함수 → 모두 보존

### ai/ai_analyzer.py
- master: 출력 메시지 쉬운 말 변환
- son-dev: 입력 프롬프트 초보자 친화 재작성
- 결과: 입력/출력 양쪽 개선 모두 적용

## 검증
- py_compile: OK
- 핵심 임포트: OK
- pytest: 43 + master 신규 = NN passed
- session_start_check.py: OK
- JSON 유효: OK

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"

# push
git push origin son-dev

# PR #1 자동 업데이트됨
```

---

## 6. PR #1 머지 가능 확인 + 머지 실행

```bash
# 잠시 대기 후 mergeable 재확인
sleep 5
gh pr view 1 --json mergeable,mergeStateStatus
# 예상: {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
```

`mergeable: MERGEABLE` 확인되면:

```bash
# 머지 실행 (merge commit 방식 — 히스토리 보존)
gh pr merge 1 --merge --subject "Merge PR #1: son-dev 4대 엔지니어링 가이드 적용 + 리뷰 5건 대응"

# 또는 squash 원하면 (히스토리 단순화):
# gh pr merge 1 --squash

# son-dev 브랜치는 유지 (계속 작업하므로 --delete-branch 옵션 사용 X)
```

머지 후 자동 출력:
```
✓ Merged pull request #1 (...)
```

---

## 7. 머지 후 후속 작업

### 7-1. master 최신화 확인

```bash
git fetch origin
git log --oneline origin/master | head -5
# 최상단에 PR #1 머지 커밋이 보여야 함
```

### 7-2. son-dev 자체도 master 최신화 (다음 작업 위해)

```bash
git checkout son-dev
git pull origin son-dev
# son-dev에는 머지 커밋이 이미 들어가 있음 (PR #1 head)
```

### 7-3. 아빠 PC에 알림

머지 완료 보고. 아빠 PC가 다음을 수행해야 함:
```bash
# 아빠 PC (dad-dev 체크아웃 상태)
git fetch origin
git checkout dad-dev
git merge origin/master
# 충돌 발생 가능 → 별도 지시서 필요할 수 있음
```

---

## 8. 트러블슈팅

### Q: 충돌 해결 중 어디까지 했는지 모르겠음
```bash
git status                          # 충돌 파일 목록
git diff --name-only --diff-filter=U  # 미해결 충돌 파일만
```

### Q: 충돌 너무 복잡해서 처음부터 다시
```bash
git merge --abort
# 머지 시작 전 상태로 복구. 다시 시도.
```

### Q: 보호 파일 `position_manager.py` 수정 차단됨 (PreToolUse 훅)
- 메시지 확인 후 사용자에게 보고
- 사용자 승인 시 → 수정 진행
- 자동 진행 금지

### Q: pytest 실패 (master 신규 테스트가 son-dev 코드 기대)
- 어느 테스트가 실패했는지 명시
- master 신규 테스트 vs son-dev 신규 코드의 충돌 가능성
- 3회 시도 후 해결 안 되면 사용자 보고

### Q: `git push` 실패 (rejected)
```bash
# 누군가 동시 push했을 수 있음
git pull --rebase origin son-dev
# 충돌 없으면 자동 해결
git push origin son-dev
```

### Q: `gh pr merge` 실패 (still conflicting)
- mergeable 재확인: `gh pr view 1 --json mergeable`
- master에 또 새 커밋이 있을 수 있음 → 위 5번부터 다시
- GitHub UI에서 직접 머지 가능한지 확인

### Q: 머지 후 master에 PR #1이 안 보임
```bash
git fetch origin
git log --oneline origin/master | grep "Merge pull request #1"
```
- 안 보이면 GitHub PR 페이지에서 상태 확인
- "Closed without merge"라면 머지 실패

### Q: 보호 파일 차단으로 작업 못 함
- `.claude/settings.json` PreToolUse 보호 파일 5종이 있음
- merge 중 자동 수정은 git이 하므로 hook 발동 안 함 (Edit 도구 안 씀)
- 단, 수동 편집 시(Edit tool) 차단됨 → 사용자 승인 후 진행

---

## 9. 완료 보고 양식

머지 성공 시 사용자에게:

```
✅ PR #1 머지 완료 (master 통합)

## 머지 요약
- 머지 커밋: <SHA>
- 방식: merge commit
- son-dev → master 통합
- 머지 커밋 1 + son-dev 8커밋 = master에 9커밋 추가

## 충돌 해결 결과 (6파일)
- alerts/trade_executor.py: master 50만 고정 + son-dev VETO 방어선 결합
- alerts/signal_runner.py: master heartbeat + son-dev MOCK 청산 결합
- alerts/position_manager.py: 양쪽 다른 함수 → 자동 병합
- alerts/telegram_commander.py: master 메시지 + son-dev ENABLED 스위치
- kiwoom/kiwoom_collector.py: master AI 수정 + son-dev 배치 로테이션
- ai/ai_analyzer.py: master 출력 변환 + son-dev 프롬프트 재작성

## 검증
- pytest: NN passed (43 + master 신규 통합)
- 핵심 임포트: OK
- session_start_check.py: OK
- JSON: OK

## 다음 단계 (아빠 PC)
- git fetch origin
- git checkout dad-dev
- git merge origin/master
- (충돌 해결 시 별도 지시서 작성 필요할 수 있음)
- 월요일 MOCK 투입 전 모든 인프라 동기화 완료
```

머지 실패 시:
```
⚠️ PR #1 머지 실패

## 실패 원인
[구체 메시지]

## 마지막 시도 시점 상태
[git status / mergeable 출력]

## 권장 다음 단계
[옵션 제시]
```

---

## 핵심 요약 (TL;DR for Claude)

```
1. son-dev 브랜치 확인 → git pull
2. git merge origin/master
3. 6파일 충돌 해결 (각 파일 가이드 참조, 양쪽 의도 보존)
4. 검증 7개 (마커/문법/임포트/pytest/스크립트/JSON)
5. git commit + push
6. gh pr merge 1 --merge
7. 완료 보고
```

**원칙**:
- 양쪽 의도 모두 보존 (한쪽 버리지 말 것)
- 보호 파일은 사용자 확인 필수
- 검증 실패 시 중단 + 보고
- 임의 판단 금지 (애매하면 보고)
