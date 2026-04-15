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
   - **Tier 1 (직접)**: 변경 파일을 직접 import
   - **Tier 2 (간접)**: Tier 1을 import
   - **Tier 3 (데이터)**: JSON 스키마가 바뀌는 경우

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
