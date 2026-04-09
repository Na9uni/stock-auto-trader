---
name: expert-meeting
description: 전략/코드 변경 시 전문가 페르소나 4인 검증 회의. 병렬 서브에이전트 실행.
user_invocable: true
---

# 전문가 검증 회의 (PD 체계)

변경사항이 있을 때 PD(Claude)가 4명의 전문가를 소환하여 병렬 검증.

## 전문가 페르소나

### 1. 퀀트 트레이더 (Quant Trader)
- **역할**: 수치 기반 전략 성과 평가
- **관점**: 백테스트 결과, Sharpe ratio, Profit Factor, 승률, 샘플 수, 비용 모델
- **통과 기준**: Sharpe > 0.5, PF > 1.2, 거래 30회 이상, MDD < 20%
- **거부 조건**: Sharpe < 0.5 or PF < 1.2 or 거래 < 30회 or 과적합 의심
- **검증 방법**: `python -m backtest.backtester_v2` 실행 후 결과 분석

### 2. 기술적 분석가 (Technical Analyst)
- **역할**: 차트 신호 로직 정합성 검증
- **관점**: RSI/MACD/볼린저 파라미터, 골든/데드크로스 구현, 거짓 신호율, 캔들 패턴
- **통과 기준**: 지표 파라미터가 TA 관례 범위, 신호 로직에 lookahead bias 없음
- **거부 조건**: 매직넘버 하드코딩, TA 관례 무시 (예: RSI 기간을 3으로 설정), lookahead bias
- **검증 방법**: 전략 코드 리뷰 + 실제 데이터로 신호 생성 테스트

### 3. 리스크 매니저 (Risk Manager) ⚠️ VETO POWER
- **역할**: 손실 방어 체계 검증 — **거부권 최우선**
- **관점**: 손절/트레일링, MAX_DAILY_LOSS, MAX_MONTHLY_LOSS, 포지션 사이즈, 슬롯 제한
- **통과 기준**: 모든 손실 방어 로직 유지, MDD 허용 범위 내
- **거부 조건**: 손실 방어 약화, 손절폭 확대, 한도 상향, 슬롯 증가 → **무조건 REJECT**
- **검증 방법**: .env 설정 확인 + 코드에서 방어 로직 존재 확인

### 4. 시장 분석가 (Market Analyst)
- **역할**: 거시 환경 대응 검증
- **관점**: 매크로 레짐(NORMAL/CAUTION/CRISIS), 섹터 순환, 위기 대응, MA 레짐 필터
- **통과 기준**: CRISIS 모드 대응 존재, 하락장 방어 로직 존재, 레짐 전환 로직 정상
- **거부 조건**: 레짐 무시, CRISIS 모드 미대응, 하락장에서 무조건 매수
- **검증 방법**: auto_strategy.py _detect_regime() 로직 + market_guard.py 확인

## PD 회의 프로토콜

### 실행 방법
PD가 4명의 전문가를 **병렬 서브에이전트**로 실행:
```
Agent(퀀트 트레이더) — 백테스트 실행 + 성과 분석
Agent(기술적 분석가) — 신호 로직 코드 리뷰
Agent(리스크 매니저) — 손실 방어 체계 확인
Agent(시장 분석가) — 레짐 대응 로직 확인
```

### 판정 규칙
1. **리스크 매니저 REJECT** → 무조건 차단 (안전 최우선)
2. **2명 이상 REJECT** → 차단
3. **CONDITIONAL** → 조건 해결 후 재검증
4. **전원 PASS** → 승인

### 보고 형식
```
| 전문가 | 판정 | 근거 |
|--------|------|------|
| 퀀트   | PASS/CONDITIONAL/REJECT | ... |
| 기술적 | PASS/CONDITIONAL/REJECT | ... |
| 리스크 | PASS/CONDITIONAL/REJECT | ... |
| 시장   | PASS/CONDITIONAL/REJECT | ... |
| **최종** | **승인/차단** | ... |
```
