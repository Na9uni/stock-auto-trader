# 주식 자동매매 시스템 v2 -- 프로젝트 현황

## GitHub 저장소
https://github.com/Na9uni/stock-auto-trader (private)

## 아키텍처
- 2-프로세스: 32-bit 키움 수집기(PyQt5/COM) + 64-bit 스케줄러/분석
- IPC: JSON 파일 (kiwoom_data.json, order_queue.json, auto_positions.json)
- 키움 OpenAPI+: 30초 폴링 + 실시간 체결/호가(SetRealReg)

## 파일 구조 (v2 재설계)
```
stock/
├── config/                        # [NEW] 설정 통합
│   ├── trading_config.py          # TradingConfig (frozen dataclass, .env 로드)
│   └── whitelist.py               # 화이트리스트 + ETF/개별주 분류
├── strategies/                    # [NEW] 전략 플러그인
│   ├── base.py                    # Strategy 프로토콜, MarketContext, SignalResult
│   ├── vb_strategy.py             # 변동성 돌파 (주 전략)
│   ├── score_strategy.py          # 합산 전략 (거부권 모드)
│   └── combo_strategy.py          # VB + 거부권 조합
├── alerts/
│   ├── analysis_scheduler.py      # 메인 스케줄러 (전략 연결 완료)
│   ├── signal_detector.py         # RSI/MACD/볼린저 합산 (거부권용)
│   ├── volatility_breakout.py     # 레거시 VB (vb_strategy로 대체)
│   ├── telegram_commander.py      # 텔레그램 명령 처리
│   └── telegram_notifier.py       # 텔레그램 알림 발송
├── analysis/
│   └── indicators.py              # 기술적 지표 27개
├── trading/
│   ├── auto_trader.py             # 매수/매도 실행 (TradingConfig 기반)
│   ├── kiwoom_order_queue.py      # JSON IPC 주문 큐
│   └── targets.py                 # 목표가/손절가 감시
├── backtest/
│   ├── backtester_v2.py           # [NEW] 통합 백테스터 (비용 현실화)
│   ├── cost_model.py              # [NEW] 실전 비용 모델 (보호지정가 반영)
│   ├── exit_manager.py            # [NEW] 실전 동일 청산 로직
│   ├── backtester.py              # 레거시 백테스터
│   ├── backtest_vb.py             # 레거시 VB 백테스트
│   ├── optimize.py                # 그리드 서치 최적화
│   └── run_backtest.py            # 단일 종목 백테스트
├── kiwoom/
│   └── kiwoom_collector.py        # 32-bit 키움 데이터 수집기
├── ai/
│   └── ai_analyzer.py             # Claude Sonnet AI 판단
├── data/                          # JSON 데이터 파일들
├── ui/
│   └── dashboard.py               # Streamlit 대시보드
├── utils/
│   └── tick_size.py               # 호가 단위 맞춤
└── .env                           # 환경변수
```

## 전략 (v2: 변동성 돌파 + 합산 거부권)

### 주 전략: 변동성 돌파
- 목표가 = 시가 + 전일레인지 x K (ETF: 0.5, 개별주: 0.6)
- 마켓 필터: 시가 > MA10
- 시장 레짐 필터: MA20 > MA60 (상승장만 진입)
- 변동성 필터: 전일 레인지 >= 시가의 0.5%
- 청산: 트레일링 스탑 전용 (익일 시가 매도 폐지)

### 보조: 합산 거부권
- 기존 14개 지표 합산 점수 < -3이면 변동성 돌파 매수 거부
- 단독 매수 불가 (거부권만 발동)

### 봉 확정 체크
- 합산 신호: 5분 경계(00/05/10/15...) 직후 30초 이내에만 감지
- 변동성 돌파: 매분 체크 (일봉 기반이라 미확정 문제 없음)

## 설정 (.env) -- v2 변경 사항
```
# 리스크 (대폭 축소)
STOPLOSS_PCT=2.0          # 3.5% -> 2.0%
TRAILING_ACTIVATE_PCT=2.5 # 3.0% -> 2.5%
TRAILING_STOP_PCT=1.0     # 1.5% -> 1.0%
MAX_SLOTS=2               # 3 -> 2
AUTO_TRADE_AMOUNT=250000  # 500000 -> 250000

# 손실 한도 (대폭 축소)
MAX_DAILY_LOSS=30000      # 300000 -> 30000 (3%)
MAX_MONTHLY_LOSS=100000   # 1000000 -> 100000 (10%)
MAX_CONSEC_STOPLOSS=2     # 3 -> 2

# 전략
STRATEGY=combo            # [NEW] vb | score | combo
VB_K=0.5                  # [NEW] ETF K값
VB_K_INDIVIDUAL=0.6       # [NEW] 개별주 K값
BUY_START_MINUTE=10       # [NEW] 장 시작 10분 이후
BUY_END_HOUR=14           # [NEW] 14시 이후 매수 차단
```

## 백테스트 v2 결과 (비용 현실화)

### 비용 모델 (이전 백테스트 vs v2)
| 항목 | 이전 | v2 |
|------|------|-----|
| 보호지정가 | 미반영 | 0.3~1.0% 반영 |
| 왕복 비용 | 0.21% | 0.6~2.2% |
| 체결 가정 | 목표가 즉시 | next-bar + 보호마진 |
| 분할 익절 | 미반영 | 50% @ +2.5% |
| 시장 레짐 | 없음 | MA20>MA60 필터 |

### 최근 1년 (상승장)
| 종목 | 전략 | BnH | 거래 | 승률 | PF | MDD | Sharpe |
|------|------|-----|------|------|-----|-----|--------|
| KODEX200 | +0.0% | +141% | 63 | 68% | 1.0 | 20% | 0.11 |
| TIGER나스닥100 | +2.7% | +38% | 20 | 60% | 1.2 | 9% | 0.32 |
| 삼성전자 | +18.2% | +230% | 56 | 68% | 1.4 | 12% | 1.03 |
| 두산에너빌 | +26.3% | +287% | 53 | 57% | 1.4 | 20% | 0.97 |
| 일진전기 | +14.2% | +179% | 47 | 53% | 1.2 | 20% | 0.48 |
| **합산** | **+35만원** | | | | | | |

### 2022 하락장
| 종목 | 전략 | BnH | 거래 | 승률 |
|------|------|-----|------|------|
| KODEX200 | -9.2% | -20.9% | 4 | 0% |
| TIGER나스닥100 | -3.5% | -24.3% | 8 | 50% |
| 삼성전자 | -7.5% | -25.7% | 6 | 33% |
| **합산** | **-81만원** | | | |

하락장에서 BnH보다 손실 적음 (방어 효과 확인).
거래 횟수 대폭 감소 (레짐 필터 효과).

## 남은 과제
1. 하락장 추가 방어 (MA20 < MA120이면 매매 완전 중단)
2. ATR 기반 동적 손절 (고정 2% -> 1.5xATR)
3. K값 종목별 최적화 (walk-forward 검증)
4. 소형주(보성파워텍 등) 비용 2.2% -> 화이트리스트 재검토
5. 5분봉 데이터 축적 후 장중 백테스트 전환

## 실전 거래 기록
| 날짜 | 종목 | 보유시간 | 손익 | 사유 |
|------|------|---------|------|------|
| 3/27 | 대주전자재료 | 수시간 | +2,700 | 트레일링 |
| 3/27 | 삼성전자 | 수시간 | -13,047 | 손절 |
| 4/1 | 티에이치엔 | 20분 | -770 | 신호 뒤집힘 |
| 4/1 | 한올바이오 | 2시간 | -500 | 시간청산 |
| 3/31 | KODEX코스닥150 | 이틀 | -2,685 | 체결 인식 실패 |

## 현재 보유 (사용자 수동매매)
- 003280 흥아해운 14주 (manual)
- 005930 삼성전자 3주 (manual)
- 014790 HL D&I 18주 (manual)
