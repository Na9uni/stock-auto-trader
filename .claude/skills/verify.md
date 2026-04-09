---
name: verify
description: 프로젝트 전체 검증 (임포트, 문법, 핵심 모듈)
user_invocable: true
---

# Verify Skill

프로젝트 전체를 검증합니다.

## Steps

1. 모든 .py 파일 문법 체크:
   ```bash
   find . -name "*.py" -exec python -m py_compile {} \;
   ```

2. 핵심 모듈 임포트 검증:
   ```bash
   python -c "
   from config.trading_config import TradingConfig
   from strategies.combo_strategy import ComboStrategy
   from strategies.vb_strategy import VBStrategy
   from strategies.score_strategy import ScoreStrategy
   from alerts.analysis_scheduler import *
   from analysis.indicators import *
   print('All imports OK')
   "
   ```

3. .env 파일 존재 확인
4. 오류 발생 시 원인 분석 및 수정 제안

## 보고 형식

- OK: 통과 항목 수
- FAIL: 실패 항목 + 원인 + 수정 방법
