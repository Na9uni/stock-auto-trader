---
name: add-strategy
description: 새로운 매매 전략 추가
user_invocable: true
---

# Add Strategy Skill

새로운 매매 전략을 추가합니다.

## Steps

1. `strategies/base.py`에서 Strategy Protocol, MarketContext, SignalResult 확인
2. `strategies/` 디렉토리에 새 전략 파일 생성
3. Strategy Protocol 구현 (`name`, `evaluate()`)
4. 기존 전략 참고: `strategies/vb_strategy.py`
5. 임포트 검증: `python -c "from strategies.<new_module> import <NewStrategy>"`
6. 백테스트 실행하여 성과 검증

## Rules

- 반드시 `SignalResult`를 반환해야 함
- `MarketContext`의 데이터만 사용 (외부 API 직접 호출 금지)
- 손실 방어 로직은 전략이 아닌 `trading/auto_trader.py`에서 처리
- 상세 구조: `docs/strategies.md` 참조
