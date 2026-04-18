# backtest/ — 백테스터 + 전략 비교 도구

**역할**: 실전 비용 모델로 전략 성과 측정. 운영 전/후 검증 필수.

## 백테스터 종류

| 스크립트 | 용도 | 비교 대상 |
|---|---|---|
| `backtester_v2.py` | VB 단독, 가장 정밀 | 단일 전략 |
| `backtester_auto.py` | AUTO 전체 (레짐 + VB + trend + crisis_mr + 거부권) | 실전 로직 시뮬 |
| `compare_strategies.py` | VB vs Trend | 전략 선택 |
| `compare_vb_filters.py` | VB 기본 vs 엄격필터 vs 완화필터 | 필터 효과 측정 |
| `compare_all_years.py` | 연도별 성과 | 일관성 검증 |
| `walk_forward_auto.py` | walk-forward (in/out sample) | 과적합 검증 |
| `optimize.py` / `k_optimizer.py` | K값 최적화 | 그리드서치 |

## 실행 규칙

```bash
PYTHONIOENCODING=utf-8 python -m backtest.<스크립트>
```
- 한글 출력 깨짐 방지 위해 `PYTHONIOENCODING=utf-8` 권장
- 결과는 콘솔 출력. 파일 저장 필요 시 `tee`로 리다이렉트

## 결과 해석 기준 (퀀트 통과 기준)

| 지표 | 기준 | 의미 |
|---|---|---|
| Sharpe | > 0.5 | 위험 대비 수익. 한국 단타는 0.5~1.0 현실적 |
| Profit Factor | > 1.2 | 총이익/총손실. 1.5+ 양호 |
| MDD | < 20% | 최대 낙폭. 25%+는 실전 운용 어려움 |
| 거래 횟수 | ≥ 30회 | 통계적 유의성 |
| 승률 | 단독 무의미 | RR 함께 봐야 함 |

## 흔한 함정 (체크리스트)

- **Lookahead bias**: 당일 고가/저가를 장중에 사용 금지
- **Survivorship bias**: 상장폐지 종목 누락 (yfinance 한계)
- **과적합**: walk-forward 또는 in/out-sample 분리 필수
- **거래 < 30회**: 통계 신뢰도 부족 → 결과 신뢰 금지

## CRISIS 분리 백테스트 (필수)

전체 기간 평균 Sharpe만 보면 안 됨. 2022 하락장(KOSPI -30%) 단독으로:
```bash
PYTHONIOENCODING=utf-8 python -m backtest.backtester_auto
# 출력에서 "2022 하락장" 섹션 확인
```
2022 단독 Sharpe 음수면 **전체 기간 양수여도 위기 대응 부적합**.

## 전략 변경 → 백테스트 → 평가 워크플로우

1. 전략 코드 수정
2. `backtester_v2` (해당 전략 단독) 실행
3. `backtester_auto` (전체 로직) 실행
4. `compare_all_years` (연도별) 실행
5. 모든 지표 통과 시 운영 반영, 실패 시 롤백
6. **사용자 보고**: 수치 표 + 수정 전/후 비교

## 새 백테스터 추가 시

- `BacktesterV2` 클래스 또는 함수형 둘 다 OK
- 비용 모델은 `backtest/cost_model.py` 사용
- 청산 로직은 `backtest/exit_manager.py` 사용
- 결과 출력은 표 형태 (수익률 / 거래수 / 승률 / Sharpe / MDD)
