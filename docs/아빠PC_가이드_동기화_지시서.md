# 아빠 PC 클로드코드 작업 지시서 — 4대 엔지니어링 가이드 동기화

> **이 문서는 아빠 PC의 Claude Code에게 전달됩니다.**
> 사람이 읽는 게 아니라, 클로드가 순서대로 실행합니다.

---

## 0. 배경 (클로드가 이해해야 할 맥락)

아들 PC에서 4대 엔지니어링 가이드(프롬프트/콘텍스트/하네스/컴파운드)를 적용하여
다음을 개선했습니다:

1. **CLAUDE.md 슬림화** (111줄 → 74줄, ARCHITECTURE.md 분리)
2. **L4 하위 CLAUDE.md 추가** (strategies/, alerts/, backtest/ — lazy-load)
3. **SessionStart 훅 추가** (세션 시작 시 환경 자동 점검)
4. **expert-meeting 페르소나 자동 로드 강제** (오늘 직접 겪은 회의 품질 문제 해결)
5. **judge-generator 스킬 신규** (백테스트 자동 최적화 루프 가이드)
6. **리스크 매니저 VETO 반영** (5개 방어선 복구 — MOCK도 LIVE와 동일하게 보호)

이미 `son-dev` → `master` PR(#1)이 생성되어 있고, master에 merge되면
아빠 PC도 동일한 가이드/훅/스킬을 적용해야 양쪽 PC 일관성 유지.

---

## 1. 사전 확인

```bash
pwd
git remote -v
# origin이 https://github.com/Na9uni/stock-auto-trader.git 인지 확인.

git branch --show-current
# dad-dev 여야 함. 다르면 사용자에게 보고하고 멈춤.
```

`dad-dev`가 아니면 **즉시 멈추고 사용자에게 보고**.
master에 있으면 dad-dev 전환 가이드(`docs/아빠PC_클로드코드_작업지시서.md`)부터 실행.

---

## 2. PR #1 머지 확인

```bash
gh pr view 1 --json state,mergedAt 2>&1
```

- `state: MERGED` 이면 다음 단계 진행
- `state: OPEN` 이면 사용자에게 "PR #1을 GitHub에서 merge하라" 알리고 대기
- `state: CLOSED` 이면 사용자 확인 (왜 닫혔는지)

---

## 3. master 변경 받아오기

```bash
# 현재 dad-dev 작업이 commit 안 된 게 있으면 먼저 임시 커밋 (data 파일 제외)
git status --short
# modified 있으면:
git status --short | grep -vE "^\?\?|data/|\.env" | awk '{print $2}' | while read f; do
  git add "$f"
done
# .env가 staged에 들어가면 즉시 unstage:
git reset HEAD .env 2>/dev/null
git diff --cached --name-only | grep "^\.env$" && echo "❌ .env 커밋 시도 차단" && exit 1

# 임시 커밋
git diff --cached --quiet || git commit -m "chore: dad-dev 동기화 전 임시 커밋"

# master 동기화
git fetch origin
git checkout master
git pull origin master
git checkout dad-dev
git merge master
```

### merge 충돌 발생 시

**자동 해결 시도 금지.** 충돌 파일 목록을 사용자에게 보고하고 지시 대기.

특히 다음 파일에서 충돌 발생 가능:
- `CLAUDE.md` (master에서 슬림화됨)
- `.claude/settings.json` (SessionStart 훅 추가됨)
- `config/whitelist.py` (BACKTEST_VERIFIED 분리됨)

---

## 4. 적용된 변경 검증

```bash
echo "=== CLAUDE.md (가이드 권장 80줄 이하) ==="
wc -l CLAUDE.md
# 출력: 74줄 정도여야 함. 100줄 넘으면 master 동기화 실패 가능성.

echo "=== L4 CLAUDE.md 존재 확인 ==="
ls strategies/CLAUDE.md alerts/CLAUDE.md backtest/CLAUDE.md
# 3개 모두 존재해야 함.

echo "=== ARCHITECTURE.md 분리 확인 ==="
ls docs/ARCHITECTURE.md

echo "=== SessionStart 훅 확인 ==="
python -c "
import json
s = json.load(open('.claude/settings.json',encoding='utf-8'))
hooks = s.get('hooks', {})
ss = hooks.get('SessionStart', [])
assert len(ss) > 0, 'SessionStart 훅 없음'
print(f'SessionStart 훅 {sum(len(item.get(\"hooks\",[])) for item in ss)}개 OK')
"

echo "=== expert-meeting 표준 템플릿 확인 ==="
grep -c "표준 Prompt 템플릿" .claude/skills/expert-meeting.md
# 1 출력되어야 함

echo "=== judge-generator 스킬 확인 ==="
ls .claude/skills/judge-generator.md

echo "=== AUTO_TRADE_WHITELIST 7종목 확인 ==="
python -c "
from config.whitelist import AUTO_TRADE_WHITELIST, MOCK_WATCH_EXTENDED, is_watched, is_whitelisted
assert len(AUTO_TRADE_WHITELIST) == 7, f'매매 허용 7개여야 하는데 {len(AUTO_TRADE_WHITELIST)}개'
assert len(MOCK_WATCH_EXTENDED) >= 90, f'감시 확장 90+ 여야 하는데 {len(MOCK_WATCH_EXTENDED)}개'
print(f'매매 허용 {len(AUTO_TRADE_WHITELIST)}종목 / 감시 확장 {len(MOCK_WATCH_EXTENDED)}종목 OK')
"

echo "=== 테스트 ==="
python -m pytest tests/ -q --tb=short 2>&1 | tail -3
# 43 passed 나와야 함
```

위 모든 검증 통과 시 **다음 단계로**. 하나라도 실패하면 **멈추고 사용자에게 보고**.

---

## 5. 아빠 PC 전용 .env 설정 재확인

```bash
grep -E "^(TELEGRAM_COMMANDER_ENABLED|KIWOOM_MOCK_MODE|OPERATION_MODE|EOD_LIQUIDATION|MOCK_SEED_MONEY|STRATEGY)=" .env
```

아빠 PC 필수 값:
| 항목 | 값 | 이유 |
|---|---|---|
| `TELEGRAM_COMMANDER_ENABLED` | `false` | 아들 PC가 명령 수신 전담 (양쪽 true면 409 충돌) |
| `KIWOOM_MOCK_MODE` | `True` | 스윙 테스트 |
| `OPERATION_MODE` | `MOCK` | 가상매매 |
| `STRATEGY` | `auto` | 자동 전략 전환 |
| `EOD_LIQUIDATION` | `false` | 스윙 모드 |
| `MOCK_SEED_MONEY` | `1000000` | 가상 시드 100만원 |

누락 항목 있으면 사용자에게 확인 후 추가. **`.env`는 절대 커밋 금지.**

---

## 6. dad-dev 푸시

merge 결과를 원격에 반영:

```bash
git status --short
# 깨끗해야 함 (data/, .env 제외)

git push origin dad-dev
```

---

## 7. 시스템 재시작 (선택)

코드 변경이 반영되려면 Collector + Scheduler 재시작:
- 기존 실행 중인 창 모두 종료
- `run_all.bat` 재실행

재시작 후 SessionStart 훅이 자동으로 환경 점검 출력함.

---

## 8. 완료 보고 양식

모든 단계 완료 시:

```
✅ 4대 엔지니어링 가이드 동기화 완료 (아빠 PC dad-dev)

- master merge: PR #1 → dad-dev 반영 OK
- CLAUDE.md: NN줄 (가이드 권장 범위)
- L4 CLAUDE.md: 3개 추가 확인 (strategies/alerts/backtest)
- SessionStart 훅: N개 확인
- expert-meeting 표준 템플릿: 적용 확인
- judge-generator 스킬: 신규 추가 확인
- AUTO_TRADE_WHITELIST: 7종목 (BACKTEST_VERIFIED만)
- pytest: 43 passed
- .env 설정: TELEGRAM_COMMANDER_ENABLED=false 외 5개 항목 확인
- dad-dev push: 완료

다음 작업: dad-dev 브랜치에서 진행
```

---

## 9. 문제 발생 시

### Q: master에 PR이 아직 머지 안 됨
→ 사용자에게 "PR #1을 GitHub에서 merge하라" 알리고 대기.

### Q: merge 충돌
→ 충돌 파일 목록 보고 + 자동 해결 금지.

### Q: pytest 실패
→ 실패 테스트 메시지 그대로 보고. 수정 시도 금지.

### Q: AUTO_TRADE_WHITELIST가 7개가 아님
→ master 동기화 실패 가능성. `git log master --oneline -3`로 확인.

### Q: .env 변경 필요한데 어떻게?
→ 사용자에게 어떤 값을 추가/변경할지 명확히 보고 후 승인 받고 진행.

---

## 핵심 규칙

- **dad-dev 브랜치에서만 작업.** master 직접 수정 절대 금지.
- **`.env` 절대 커밋 금지.**
- **data/*.json 대부분은 .gitignore 처리됨** — 신경 쓸 필요 없음.
- **merge 충돌은 자동 해결 금지** — 반드시 사용자에게 보고.
- **테스트 실패 시 자동 수정 금지** — 사용자 보고 후 지시 대기.
