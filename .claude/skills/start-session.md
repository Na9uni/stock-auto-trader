---
name: start-session
description: 세션 시작 시 상태 확인 및 사용자 식별. "오늘 시작할게", "시작" 등의 요청에 사용.
user_invocable: true
---

# 세션 시작 스킬

## 사용자 식별

먼저 누가 사용하는지 파악:
- 기술적 요청 → 아들 (개발자 모드)
- 쉬운 말투, 기초 질문 → 아빠 (초보자 모드)
- 불확실하면 "안녕하세요! 아들이신가요, 아빠이신가요?" 물어보기

## 아빠인 경우

1. 반갑게 인사
2. 이전 세션에서 하던 작업 확인 (메모리 참조)
3. 오늘 할 수 있는 것 3가지 제안 (쉬운 말로)
4. 장 운영 시간 확인하여 안내

## 아들인 경우

1. 간결하게 프로젝트 상태 요약
2. 마지막 커밋/변경사항 확인
3. 진행 중인 과제 확인 (PROJECT_STATUS.md)

## 시스템 상태 점검

```bash
# git 상태
git status --short

# .env 존재 확인
test -f .env && echo ".env OK" || echo ".env MISSING"

# Python 확인
python --version 2>/dev/null || echo "Python NOT FOUND"
```
