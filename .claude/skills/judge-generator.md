---
name: judge-generator
description: 백테스트/검증 결과를 자동 평가하고 임계값 미달 시 파라미터 조정 후 재실행하는 self-refine 패턴. 전략 K값/필터/임계값 등 수치 최적화 시 사용.
user_invocable: true
---

# Judge-Generator (Self-Refine)

수동으로 "백테스트 → 결과 보고 → 파라미터 조정 → 재실행"을 반복하는 대신,
PD가 자동 루프를 돌려 **목표 수치 달성 또는 N회 시도 후 보고**한다.

## 적용 대상 (수치 최적화)

- 전략 파라미터 (K값, 손절폭, 트레일링)
- 필터 임계값 (MA 이격도, 거래량 배수, RSI)
- 레짐 전환 트리거 (DEFENSE -2%, CASH -3% 등)
- 기간/종목 조합

## 적용하지 말아야 할 것

- 정답이 없는 정성적 판단 (예: "코드 가독성")
- 외부 시장 환경 (백테스트로 검증 불가능)
- 휴리스틱 부족 → 무한 루프 위험 (반드시 max_iter 설정)

## 워크플로우

### 1단계: 평가 함수 정의

명확한 통과 기준을 수치로:

```
GOAL: Sharpe > 0.5 AND PF > 1.2 AND MDD < 20% AND 거래수 ≥ 30
PASS: 4개 조건 모두 충족
FAIL: 1개라도 미달 → 다음 시도
ABORT: 5회 시도 후에도 미충족 → 사용자 보고 (조건 자체가 비현실적일 수 있음)
```

### 2단계: 변수 공간 정의

조정할 파라미터와 범위:

```
vb_k: [0.3, 0.4, 0.5, 0.6, 0.7]    # 변동성 돌파 계수
filter_ma10_dev: [0.03, 0.05, 0.08] # MA10 이격도 허용
filter_volume: [1.0, 1.2, 1.5, 2.0] # 거래량 배수 임계
```

### 3단계: 탐색 전략

| 전략 | 적용 시점 | 비용 |
|---|---|---|
| Grid search | 변수 ≤ 2개, 그리드 ≤ 30 | 중간 |
| 휴리스틱 (현재값 ±20%) | 빠른 미세 조정 | 낮음 |
| Bayesian (scikit-optimize) | 변수 3+개, 비용 큰 백테스트 | 높음 |
| Random | 탐색 공간 매우 큼 | 가변 |

### 4단계: 자동 루프

```
iter = 0
best = None
while iter < MAX_ITER:
    params = next_params(strategy, history)
    result = run_backtest(params)
    if evaluate(result) == PASS:
        return params, result  # 성공
    if best is None or result.sharpe > best.sharpe:
        best = (params, result)
    history.append((params, result))
    iter += 1
return best, "MAX_ITER 도달, 최고 결과 반환"  # 부분 성공
```

### 5단계: 사용자 보고

성공/실패 무관하게:
1. 시도 횟수 + 각 시도 요약
2. 최종 선택 파라미터 + 수치
3. 통과 못한 경우: 어느 조건이 가장 못 미쳤는지
4. 권고: 운영 반영 / 추가 검증 / 목표 재조정

## 사용 예시

### 예 1: VB K값 최적화

```
GOAL: 108450 종목에서 2년간 Sharpe > 1.0
변수: vb_k ∈ {0.3, 0.4, 0.5, 0.6, 0.7}
탐색: Grid (5회)
백테스트: backtester_v2.run_vb(ticker="108450", k=...)
```

### 예 2: 고점 회피 필터 임계값

```
GOAL: 2022 하락장 -8% 이내 + 2년 수익 +30% 이상
변수:
  - filter_ma10_dev ∈ {0.03, 0.05, 0.08}
  - filter_volume ∈ {1.0, 1.2, 1.5}
탐색: Grid (3×3=9회)
백테스트: backtester_v2.run_vb(use_high_point_filters=True, ...)
```

## 안티패턴

| 안티패턴 | 왜 위험 | 대신 |
|---|---|---|
| MAX_ITER 없이 무한 루프 | 비용 폭발, 끝나지 않음 | 5~20회 한도 |
| 동일 데이터 반복 최적화 | 과적합 | walk-forward 또는 OOS 분리 |
| 한 종목/한 기간만 | 일반화 실패 | 다종목 + 다기간 |
| 통과 시 즉시 운영 | 우연한 성공 가능 | 사용자 승인 + 별도 OOS 검증 |
| 평가 기준 후행 변경 | "맞춰서 통과시키기" | 시작 전 확정 |

## REJECT 후 행동

5회 시도 후에도 통과 못 하면:
1. **MAX_ITER 도달 보고** (모든 시도 결과 표)
2. **최고 결과 + 부족한 부분** 명시
3. **3가지 선택지 제시**:
   - A) 목표 완화 (Sharpe 0.5 → 0.3)
   - B) 변수 공간 확장 (K 범위 ±0.1 더)
   - C) 전략 자체 재검토 (이 종목엔 부적합)
4. 사용자 결정 대기

## 실전 호출 예시

### VB K값 최적화 (단일 종목)
```bash
PYTHONIOENCODING=utf-8 python -m backtest.k_optimizer
# 종목별 K값 그리드서치. 출력: 각 종목의 Sharpe/PF/MDD 최적값
```

### Walk-Forward 자동 재최적화 (전 종목)
```bash
PYTHONIOENCODING=utf-8 python -m backtest.walk_forward_auto
# in/out sample 분리 → 과적합 경고 + train vs test Sharpe 비교
```

### VB 필터 A/B 비교 (2026-04-17 신규)
```bash
PYTHONIOENCODING=utf-8 python -m backtest.compare_vb_filters
# 기존 / 엄격 필터 / 완화 필터 3버전 동시 비교
# 기간별(2년/2022 하락장/2023 회복장) 승률·Sharpe·MDD 일괄 출력
```

### PD 루프 (Judge-Generator 수동 실행 패턴)
```
1. Bash 도구로 위 스크립트 N회 실행 (각 실행마다 파라미터 변경)
2. 결과 파싱 → 통과 기준 대조 (Sharpe > 0.5 등)
3. FAIL이면 다음 파라미터 조합 결정 (grid/휴리스틱)
4. 1~3 반복 (MAX_ITER=5 한도 엄수)
5. 통과 시 또는 MAX_ITER 도달 시 사용자 보고 (시도 이력 포함)
```

### 신규 도구 추가 시 (grid_search 같은)
- 새 백테스터 추가 전 `backtest/CLAUDE.md` 참조
- `BacktesterV2` 확장 or 별도 스크립트 둘 다 가능
- 결과는 표 형태(수익률/거래수/승률/Sharpe/MDD)로 통일

## 연계 스킬

- `expert-meeting`: 최적 결과를 4인 회의로 검증
- `trading-review`: 도메인 관점 추가 평가
- `backtest`: 백테스터 사용법 레퍼런스

## 구현 노트

이 스킬은 **PD가 직접 루프를 돌리는 가이드**. 자동화 스크립트가 아님.
즉 PD가 Bash로 백테스트 N회 실행하고 결과를 평가해서 다음 파라미터 결정.

전용 자동화 스크립트가 필요하면 `backtest/optimize.py` 또는 `backtest/k_optimizer.py` 활용.
