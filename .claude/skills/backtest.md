---
name: backtest
description: 백테스트 실행 및 결과 분석
user_invocable: true
---

# Backtest Skill

백테스트를 실행하고 결과를 분석합니다.

## Steps

1. `python -m backtest.backtester_v2` 실행
2. 결과 해석: 수익률, 승률, MDD, Sharpe ratio, PF
3. BnH(Buy and Hold) 대비 성과 비교
4. 개선 제안 도출

## 인자 처리

- 종목코드 지정 시: 해당 종목만 백테스트
- 미지정 시: 화이트리스트 전체 종목 백테스트
- `--period` 지정 시: 해당 기간 백테스트

## 결과 보고 형식

| 종목 | 전략수익률 | BnH | 거래횟수 | 승률 | PF | MDD | Sharpe |
를 표로 정리하여 보고합니다.
