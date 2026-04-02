# 주식 자동매매 시스템 — 프로젝트 현황

## GitHub 저장소
https://github.com/Na9uni/stock-auto-trader (private)

## 아키텍처
- 2-프로세스: 32-bit 키움 수집기(PyQt5/COM) + 64-bit 스케줄러/분석
- IPC: JSON 파일 (kiwoom_data.json, order_queue.json, auto_positions.json)
- 키움 OpenAPI+: 30초 폴링 + 실시간 체결/호가(SetRealReg)

## 파일 구조
```
stock/
├── alerts/
│   ├── analysis_scheduler.py  # 메인 스케줄러 (~1800줄)
│   ├── signal_detector.py     # RSI/MACD/볼린저 점수 합산 신호 감지
│   ├── volatility_breakout.py # 변동성 돌파 전략 (신규, 미적용)
│   ├── telegram_commander.py  # 텔레그램 명령 처리
│   └── telegram_notifier.py   # 텔레그램 알림 발송
├── analysis/
│   └── indicators.py          # 기술적 지표 27개 (RSI, MACD, BB, ADX, VWAP 등)
├── trading/
│   ├── auto_trader.py         # 매수/매도 실행 (보호 지정가)
│   ├── kiwoom_order_queue.py  # JSON IPC 주문 큐
│   └── targets.py             # 목표가/손절가 감시
├── kiwoom/
│   └── kiwoom_collector.py    # 32-bit 키움 데이터 수집기
├── ai/
│   └── ai_analyzer.py         # Claude Sonnet AI 판단
├── data/                      # JSON 데이터 파일들
├── backtest/
│   ├── backtester.py          # 기본 백테스터
│   ├── backtest_vb.py         # 변동성 돌파 백테스트
│   ├── optimize.py            # 그리드 서치 최적화
│   └── run_backtest.py        # 단일 종목 백테스트
├── ui/
│   └── dashboard.py           # Streamlit 대시보드
├── utils/
│   └── tick_size.py           # 호가 단위 맞춤
└── .env                       # 환경변수 (키움/텔레그램/Anthropic)
```

## 현재 전략 (문제 있음)
RSI/MACD/볼린저 기반 점수 합산 → STRONG 신호 → AI 판단 → 자동매매
- 실전 5거래일 결과: **전패 (-14,300원)**
- 백테스트: 상승장에서만 수익, 바이앤홀드보다 못함
- 근본 원인: 후행 지표 합산으로는 엣지가 없음

## 대안 전략 (구현됨, 미적용)
변동성 돌파 (래리 윌리엄스) + 마켓 필터(MA10)
- 상승장: +240만원 (8종목 합산, 1년)
- 하락장: -80만원
- 바이앤홀드보다는 못하지만 하락장 방어 효과 있음

## 설정 (.env)
```
STOPLOSS_PCT=3.5
TRAILING_ACTIVATE_PCT=3.0
TRAILING_STOP_PCT=1.5
MAX_SLOTS=3
OPERATION_MODE=LIVE
AUTO_TRADE_ENABLED=true
```

## 검증이 필요한 것
1. 변동성 돌파 전략을 적용해야 하는가?
2. 하락장 방어 필터 (코스피 지수 하락 시 매매 중단) 추가?
3. 거래 빈도가 너무 높음 (연 50~80회/종목) → 수수료 문제
4. STRONG >= 3은 거의 필터가 없는 것과 같음 → 신호 품질?
5. 100만원 소자본에서 자동매매가 현실적인가?

## 실전 거래 기록
| 날짜 | 종목 | 보유시간 | 손익 | 사유 |
|------|------|---------|------|------|
| 3/27 | 대주전자재료 | 수시간 | +2,700 | 트레일링 |
| 3/27 | 삼성전자 | 수시간 | -13,047 | 손절 |
| 4/1 | 티에이치엔 | 20분 | -770 | 신호 뒤집힘 |
| 4/1 | 한올바이오 | 2시간 | -500 | 시간청산(삭제됨) |
| 3/31 | KODEX코스닥150 | 이틀 | -2,685 | 체결 인식 실패 |

## 수정된 버그 (총 30건+)
- chejan 레이스 컨디션 방지
- sell_order_id 라이프사이클
- trailing_activated 영구 플래그
- 체결 인식 2-pass 매칭
- 분할 익절 + 트레일링 동시 발동 방지
- manual 포지션 슬롯 제외
- VWAP 일별 리셋
- 보호 지정가 가격대별 동적 마진
- 상한가/하한가 클리핑
- 기타 다수

## 현재 보유 (사용자 수동매매)
- 003280 흥아해운 14주 (manual)
- 005930 삼성전자 3주 (manual)
- 014790 HL D&I 18주 (manual)
