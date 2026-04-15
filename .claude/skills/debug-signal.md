---
name: debug-signal
description: 신호 감지 문제 디버깅. "신호가 안 나와", "왜 매수 안 했어" 등의 요청에 사용.
user_invocable: true
---

# Debug Signal Skill

매매 신호가 예상대로 나오지 않을 때 원인을 추적합니다.

## 체크 순서 (의존관계 순)

1. **데이터 확인**: `data/kiwoom_data.json` — 시세 데이터 정상 수신 여부
   ```bash
   python -c "import json; d=json.load(open('data/kiwoom_data.json')); print(f'종목수: {len(d)}, 최근: {list(d.keys())[:3]}')"
   ```

2. **레짐 확인**: `data/regime_state.json` — 현재 레짐 모드
   ```bash
   python -c "import json; print(json.load(open('data/regime_state.json')))"
   ```

3. **전략 평가**: 해당 전략의 `evaluate()` 직접 호출

4. **신호 감지**: `alerts/signal_runner.py` 루프 로직 확인

5. **주문 관리**: `alerts/order_manager.py` 필터링 조건 확인

6. **실행 확인**: `alerts/trade_executor.py` MOCK/LIVE 모드 확인

## 아빠인 경우

"지금 시스템이 주식을 왜 안 사는지 하나씩 확인해볼게요"로 시작.
각 단계마다 "여기는 정상이에요 ✓" / "여기서 문제가 있어요 ✗" 명확히.
