# 아빠 PC 클로드코드 작업 지시서

> **이 문서는 아빠 PC의 Claude Code에게 전달되는 지시서입니다.**
> 사람이 읽는 게 아니라, 클로드가 읽고 **순서대로 실행**합니다.

---

## 0. 배경 (클로드가 이해해야 할 맥락)

이 프로젝트(stock-auto-trader)는 **두 대의 PC**에서 공유됩니다:
- **아들 PC** (Na9uni): 메인 개발, 텔레그램 명령 수신
- **아빠 PC** (이 PC): 독립 운영, 텔레그램 알림 수신만

**기존 문제**: 두 PC 모두 `master` 브랜치에 직접 push → 동일 파일 동시 수정 → 충돌 반복.

**새 구조**: 브랜치 분리
- `master`: 양쪽 합의된 안정판 (pull만)
- `dad-dev`: **이 PC(아빠) 전용 작업 브랜치**
- `son-dev`: 아들 PC 전용 작업 브랜치

**아들 PC에서 이미 한 일** (2026-04-17):
- `master`를 최신으로 정리 (아빠의 27건 개선 + 아들의 오늘 작업 통합 준비 중)
- `dad-dev` 브랜치 원격에 생성 (origin/master 기준)
- `son-dev` 브랜치 원격에 생성 (아들 오늘 작업 백업 포함)
- 이 지시서를 포함한 가이드 문서 push

**이 PC(아빠)에서 해야 할 일**: `master` → `dad-dev`로 전환하고 앞으로 모든 작업은 `dad-dev`에서.

---

## 1. 실행 전 체크 (Claude가 직접 확인)

```bash
pwd
# 프로젝트 루트인지 확인. 아니면:
# cd ~/stock   또는 실제 프로젝트 경로

git remote -v
# origin 주소가 https://github.com/Na9uni/stock-auto-trader.git 인지 확인
```

만약 origin이 다른 주소면 **즉시 멈추고 사용자에게 보고**. 잘못된 저장소에서 작업 시작 방지.

---

## 2. 로컬 커밋 안 된 변경사항 안전 처리

```bash
git status
```

### 케이스 A: "nothing to commit, working tree clean"
→ 바로 3단계로.

### 케이스 B: modified 또는 untracked 파일 있음

**중요 구분:**
- `data/*.json` (auto_positions, monthly_loss, last_signal 등): **커밋하지 말 것**. 이 파일들은 새 구조에서 `.gitignore` 처리 대상. 로컬 유지만.
- `.env`: **절대 커밋 금지**. 
- 나머지 코드/설정 파일(`.py`, `.bat`, `.md` 등): 임시 커밋.

```bash
# 코드 변경만 스테이징 (data 파일 제외)
git status --short | grep -vE "^\?\?|data/|\.env" | awk '{print $2}' | while read f; do
  git add "$f"
done

git status
# 스테이징된 목록 확인. data/.env가 포함되면 멈추고 사용자에게 물어볼 것.

git commit -m "chore: dad-dev 전환 전 로컬 작업 임시 커밋"
```

**만약 `.env`가 staged에 있으면 절대 커밋하지 말고 unstage:**
```bash
git reset HEAD .env
```

---

## 3. 원격 정보 가져오기

```bash
git fetch origin
```

**검증:**
```bash
git branch -r
# 다음이 모두 보여야 함:
# origin/master
# origin/dad-dev   ← 이 PC가 이동할 브랜치
# origin/son-dev
```

`origin/dad-dev`가 안 보이면 **멈추고 보고**. 아들 PC에서 푸시가 안 된 상태.

---

## 4. dad-dev 브랜치로 전환

```bash
git checkout -b dad-dev origin/dad-dev
```

**검증:**
```bash
git branch --show-current
# 출력: dad-dev
```

---

## 5. 2단계에서 임시 커밋한 게 있다면 dad-dev로 이전

2단계에서 "chore: dad-dev 전환 전" 커밋을 만들었다면, 이건 로컬 `master`에만 있고 원격엔 없음. `dad-dev`로 옮겨야 함.

```bash
# master에 있는 로컬 전용 커밋 확인
git log master --oneline --not origin/master
```

**출력이 있으면:**
```bash
# 각 커밋 해시를 dad-dev로 cherry-pick
# 예: abc1234가 "chore: dad-dev 전환 전" 커밋이면:
git cherry-pick abc1234

# 충돌 나면 파일 확인 후 해결
git status
# 해결 후:
git add <해결된 파일>
git cherry-pick --continue
```

**출력이 없으면** (임시 커밋 안 만들었으면) 이 단계 스킵.

마지막으로 로컬 master를 원격과 동기화:
```bash
git checkout master
git reset --hard origin/master
git checkout dad-dev
```

---

## 6. `.env` 검증 (PC별 독립 설정)

이 PC(아빠)의 `.env`에 다음 항목이 제대로 설정됐는지 확인:

```bash
grep -E "^(TELEGRAM_COMMANDER_ENABLED|KIWOOM_MOCK_MODE|OPERATION_MODE|MOCK_SEED_MONEY|STRATEGY|EOD_LIQUIDATION)=" .env
```

**이 PC(아빠) 필수 값:**
| 항목 | 값 | 이유 |
|------|------|------|
| `TELEGRAM_COMMANDER_ENABLED` | `false` | 아들 PC가 명령 수신 전담. 양쪽 다 true면 텔레그램 409 충돌 |
| `KIWOOM_MOCK_MODE` | `True` | 스윙 테스트 중 |
| `OPERATION_MODE` | `MOCK` | 가상매매 |
| `STRATEGY` | `auto` | 자동 전략 전환 (고점 매수 VB 회피) |
| `EOD_LIQUIDATION` | `false` | 스윙 모드 (당일 청산 안 함) |
| `MOCK_SEED_MONEY` | `1000000` | 가상 시드 100만원 |

누락된 항목 있으면 사용자에게 확인 후 추가:
```bash
# 예: TELEGRAM_COMMANDER_ENABLED가 없으면
echo "" >> .env
echo "TELEGRAM_COMMANDER_ENABLED=false" >> .env
```

**절대 `.env`를 커밋하지 말 것.**

---

## 7. 새 브랜치 구조 작업 규칙 설정

이 PC에서 앞으로 다음 규칙 준수:

### 절대 금지
- ❌ `git push origin master` — master 직접 push 금지
- ❌ `git checkout master` 후 수정 — master에서 작업 금지

### 허용 작업
- ✅ `dad-dev`에서 코드 수정 → `git add` → `git commit` → `git push`
- ✅ 아들 작업 가져오기: `git fetch && git merge origin/master` (dad-dev에서)

### master 업데이트 필요 시 (공유 안정판 반영)
```bash
git fetch origin
git checkout master
git pull                 # 원격 master 최신 받기
git checkout dad-dev
git merge master         # dad-dev에 master 반영
git push                 # dad-dev 푸시
```

---

## 8. 최종 검증

```bash
echo "=== 현재 브랜치 (dad-dev여야 함) ==="
git branch --show-current

echo "=== dad-dev 추적 상태 ==="
git rev-parse --abbrev-ref HEAD@{upstream}
# 출력: origin/dad-dev

echo "=== 로컬 data 파일 유지 확인 ==="
ls data/auto_positions.json data/monthly_loss.json 2>/dev/null
# 파일 존재해야 함 (포지션/손실 기록 보존)

echo "=== 로컬 워킹 디렉토리 상태 ==="
git status --short
# 깨끗하거나, data/.env만 남아있어야 함

echo "=== pytest ==="
python -m pytest tests/ -q --tb=short 2>&1 | tail -3
# 모두 통과해야 함
```

모든 검증 통과하면 **완료 보고**. 하나라도 실패하면 **멈추고 사용자에게 상세 보고**.

---

## 9. 시스템 재시작 (선택적)

코드가 업데이트됐으니 Collector + Scheduler 재시작 필요:
- 기존 실행 중인 창 모두 종료
- `run_all.bat` 재실행

---

## 10. 문제 발생 시 대응

### Q: `git fetch origin`에서 인증 실패
→ 사용자에게 GitHub PAT/SSH 키 확인 요청. 임의로 credential 수정 금지.

### Q: `git checkout -b dad-dev origin/dad-dev`에서 "already exists"
→ 로컬에 이미 dad-dev 있음:
```bash
git checkout dad-dev
git pull origin dad-dev
```

### Q: merge 충돌 발생
→ **자동 해결 시도 금지**. 사용자에게 어떤 파일 충돌인지 보고하고 지시 대기.

### Q: data 파일이 tracked 상태 (git add에 포함됨)
→ index에서만 제거 (파일 유지):
```bash
git rm --cached data/auto_positions.json data/last_signal.json
```

### Q: 예상치 못한 에러
→ `git stash push -u -m "emergency-backup"`으로 먼저 백업하고 사용자에게 보고.

---

## 완료 보고 양식

모든 단계 완료 시 사용자에게 다음 형식으로 보고:

```
✅ 아빠 PC dad-dev 전환 완료

- 브랜치: dad-dev (원격 추적 OK)
- 로컬 임시 커밋: [있음/없음]
- .env 검증: [TELEGRAM_COMMANDER_ENABLED=false 외 X개 확인됨]
- data 파일 로컬 유지: OK
- pytest: [74 passed] 통과
- 다음 작업: dad-dev 브랜치에서 진행
```

---

## 핵심 요약 (TL;DR for Claude)

1. `git fetch origin`
2. `git checkout -b dad-dev origin/dad-dev`
3. `.env` 확인 (`TELEGRAM_COMMANDER_ENABLED=false` 필수)
4. 앞으로 **dad-dev 브랜치에서만** 커밋/푸시
5. master 직접 수정 절대 금지
