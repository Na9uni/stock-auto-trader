# 브랜치 분리 — 아빠 PC 설정 가이드

작성일: 2026-04-17
작성자: 아들 (son-dev 브랜치)

---

## 왜 이렇게 바뀌는가?

기존: 아빠 PC + 아들 PC 둘 다 `master`에 직접 push
→ **같은 파일을 양쪽에서 수정 → 충돌 지옥**

새 구조: 각 PC가 전용 브랜치에서만 작업
→ **충돌 원천 차단**

```
master        ← 양쪽 합의된 안정판 (둘 다 pull만)
 ├─ dad-dev   ← 아빠 PC 작업 전용
 └─ son-dev   ← 아들 PC 작업 전용
```

---

## 아빠 PC에서 해야 할 일 (순서대로)

### 0단계: 현재 작업 안전하게 보관

터미널에서 stock 폴더로 이동:

```bash
cd ~/stock   # 또는 실제 경로
```

**로컬에 커밋 안 한 변경사항이 있으면** (현재 `git status`에서 modified 뜨는 파일들):

```bash
git status
# 만약 modified 파일이 있으면:
git add .
git commit -m "chore: dad-dev 전환 전 로컬 작업 임시 커밋"
```

이러면 현재 master에 커밋되지만, 곧 dad-dev 브랜치로 옮겨갈 거라 안전.

---

### 1단계: 원격 정보 가져오기

```bash
git fetch origin
```

아들이 방금 푸시한 새 브랜치(`son-dev`, `dad-dev`) 정보를 받아옴.

---

### 2단계: dad-dev 브랜치 체크아웃

```bash
git checkout -b dad-dev origin/dad-dev
```

이러면:
- `dad-dev` 로컬 브랜치 생성
- 원격 `origin/dad-dev`와 연결
- 앞으로 커밋 → push 시 자동으로 dad-dev로 감

확인:

```bash
git branch --show-current
# 출력: dad-dev
```

---

### 3단계: 0단계에서 임시 커밋했던 것을 dad-dev로 이동

0단계에서 임시 커밋을 만들었다면, 그건 **master 로컬 브랜치에만 있고 원격엔 없음**. dad-dev로 옮기자:

```bash
# master에 있는 내 로컬 커밋이 뭔지 확인
git log master --oneline -5
# 원격에 없는 커밋이 있으면 그 커밋 해시 기억 (예: abc1234)

# dad-dev 브랜치에서 cherry-pick
git cherry-pick abc1234
```

임시 커밋 없었으면 이 단계 스킵.

---

### 4단계: 앞으로의 작업 규칙

**절대 하지 말 것:**
- ❌ `git push origin master` ← master 직접 수정 금지
- ❌ `git checkout master && 수정 && commit` ← master에서 수정 금지

**올바른 흐름:**

1. 항상 `dad-dev` 브랜치에서 작업:
   ```bash
   git checkout dad-dev
   # 파일 수정
   git add .
   git commit -m "작업 내용"
   git push
   ```

2. 안정판이 필요하면 master에서 pull만:
   ```bash
   git checkout master
   git pull   # 아들/아빠 작업이 통합된 최신판 받기
   git checkout dad-dev
   git merge master   # dad-dev에 master 변경 반영
   ```

---

### 5단계: .env 확인 (PC별 독립 설정)

아빠 PC `.env`의 이 항목들은 **아들 PC와 달라야 함**:

```
TELEGRAM_COMMANDER_ENABLED=false   # 아빠 PC는 false (텔레그램 명령 수신 꺼짐)
MOCK_SEED_MONEY=1000000            # 필요시 설정
```

**TELEGRAM_COMMANDER_ENABLED가 중요한 이유**:
- 두 PC가 같은 텔레그램 봇 토큰 사용
- 둘 다 polling하면 **409 Conflict 에러** 발생
- 한 쪽(아들 PC)만 true로 설정 → 명령 수신 전담
- 다른 쪽(아빠 PC)은 false → 알림 송신만 (정상)

---

## 두 PC 간 코드 공유하는 법

### 아빠가 작업한 걸 아들 PC로 가져가기

아빠:
```bash
git checkout dad-dev
git add .
git commit -m "feat: 새 기능"
git push
```

그 후 Pull Request로 master에 merge 요청:
- GitHub 웹에서 "New pull request" → `dad-dev` → `master`
- 아들이 검토하고 merge

또는 **급하게 아들 PC에 당장 반영**해야 하면:
- 아들 PC에서:
  ```bash
  git fetch origin
  git checkout son-dev
  git merge origin/dad-dev   # 아빠 작업 통합
  ```

### 아들이 작업한 걸 아빠 PC로 가져가기

위와 반대로:
- 아들이 `son-dev` push → PR로 master merge
- 아빠가 `git checkout master && git pull && git checkout dad-dev && git merge master`

---

## 문제 발생 시

### Q: push가 거부됨 ("non-fast-forward" 에러)
→ 원격에 내가 모르는 새 커밋이 있음. 먼저 pull:
```bash
git pull --rebase origin dad-dev
git push
```

### Q: master에 실수로 커밋함
→ 그 커밋을 dad-dev로 옮기기:
```bash
git log master --oneline -3   # 실수한 커밋 해시 확인
git checkout dad-dev
git cherry-pick <해시>
git checkout master
git reset --hard origin/master   # master 원래대로
```

### Q: 충돌이 뭔지 모르겠어
아들한테 물어봐. 혼자 해결하려다 일 키우지 말 것.

---

## 요약 (1줄)

**아빠 PC는 이제부터 `dad-dev` 브랜치에서만 작업. `git checkout -b dad-dev origin/dad-dev` 한 번 실행 후 앞으로 그냥 커밋/푸시하면 됨.**
