# Architecture

## 2-프로세스 구조

```
[32-bit 프로세스]                    [64-bit 프로세스]
kiwoom_collector.py                  analysis_scheduler.py
  ├─ PyQt5 QAxWidget (COM)             ├─ 전략 평가 (ComboStrategy)
  ├─ 30초 폴링 + 실시간 체결            ├─ 포지션 관리
  ├─ kiwoom_data.json 쓰기             ├─ 자동매매 실행
  └─ order_queue.json 읽기 → 주문      ├─ 텔레그램 알림/명령
                                       └─ AI 분석 (Claude API)
         ◄── JSON IPC ──►
```

## IPC 파일

| 파일 | 방향 | 내용 |
|------|------|------|
| `kiwoom_data.json` | 수집기 → 스케줄러 | 실시간 시세, 잔고 |
| `order_queue.json` | 스케줄러 → 수집기 | 매수/매도 주문 |
| `auto_positions.json` | 양방향 | 자동매매 포지션 |

## Atomic Write 패턴

```python
import tempfile, os, json

def atomic_write(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)  # atomic on same filesystem
```

## 전략 구조

```
Strategy (Protocol)
  ├─ VBStrategy        # 변동성 돌파 (주 전략)
  ├─ ScoreStrategy     # 합산 점수 (거부권)
  └─ ComboStrategy     # VB + Score 조합
```

ComboStrategy 흐름:
1. VB가 BUY 신호 발생
2. Score 점수 확인 (≤ -3이면 거부)
3. 거부 안 되면 매수 실행

## 리스크 관리

- 일일 손실 한도: `MAX_DAILY_LOSS`
- 월간 손실 한도: `MAX_MONTHLY_LOSS`
- 연속 손절 한도: `MAX_CONSEC_STOPLOSS`
- 트레일링 스탑: 활성화 → 하락 시 자동 매도
