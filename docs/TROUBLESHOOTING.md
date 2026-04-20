# 문제 해결 가이드 (Troubleshooting)

자동매매 시스템에서 문제가 생겼을 때 **증상으로 검색**해서 해결하세요.

## 📋 빠른 진단

문제가 생기면 **먼저 이것부터**:

```
py -3.11 scripts\health_check.py
```

환경 점검 결과가 바로 나옵니다. FAIL 있으면 그 항목부터 고치세요.

---

## 🔁 실행/파일 구조

시스템은 **bat 파일 ↔ python 스크립트** 로 분리되어 있습니다.

| 역할 | bat (실행 진입) | python (실제 로직 호출) | 비트 |
|------|----------------|------------------------|-----|
| 전체 시작 | `run_all.bat` | health_check → 2개 창 | — |
| 수집기 | `run_collector.bat` | `scripts/start_collector.py` | 32 |
| 분석기 | `run_scheduler.bat` | `scripts/start_scheduler.py` | 64 |
| 대시보드 | `run_dashboard.bat` | `scripts/start_dashboard.py` | 64 |
| 자가진단 | (없음) | `scripts/health_check.py` | 64 |

**규칙** (재발 방지):
- 모든 bat은 **python -c "..." 금지** (이스케이프 버그)
- 32-bit는 `py -3.11-32`, 64-bit는 `py -3.11` 명시 (그냥 `python` 금지)
- 진입 스크립트는 **`scripts/` 밑에 분리** (sys.path 자동 추가)

---

## 🚨 증상별 해결

### 1) kiwoom_data.json이 갱신 안 됨 / Scheduler가 "수집기 죽음" 알림

**증상**: 텔레그램에서 "⚠️ 키움 수집기 응답 없음" 알림이 반복됨.

**원인 후보**:
- (A) Collector 창이 꺼짐
- (B) 잘못된 폴더에서 실행 중 (Desktop\stock 등 중복 폴더)
- (C) 키움 OpenAPI 로그인 끊김

**해결**:
1. `py -3.11 scripts\health_check.py` → `바탕화면 중복` 항목 FAIL?
   - 해결: `powershell -ExecutionPolicy Bypass -File scripts\delete_desktop_stock.ps1`
2. 작업관리자에서 python 프로세스 전부 종료
3. `C:\stock\run_all.bat` 더블클릭 (**반드시 C:\stock 폴더 것**)

**2026-04-20 장애 사례**: 바탕화면에 `C:\Users\…\Desktop\stock` 구버전 폴더가 있어서 아빠가 그 폴더의 bat를 실행 → 데이터가 엉뚱한 곳에 써짐.

---

### 2) Scheduler 창에 `SyntaxError: invalid syntax` (python 한 단어만 나옴)

**증상**: Scheduler 검은 창에 `File "<string>", line 1 / from / SyntaxError` 로 바로 죽음.

**원인**: bat 파일 안에서 `python -c "from ... "` 의 이중 따옴표 이스케이프가 cmd에 의해 깨짐.

**해결**:
- `run_*.bat` 에서 `python -c "..."` 구문을 **절대 쓰지 않기**
- 실행할 코드가 있으면 `scripts/start_*.py` 로 분리하고 `py -3.11 scripts\start_*.py` 로 호출

**관련 파일**: `run_all.bat`, `run_scheduler.bat`, `scripts/start_scheduler.py`

---

### 3) Scheduler가 32-bit python으로 실행됨

**증상**: `scripts/health_check.py` 에서 확인 가능. 또는 메모리 부족/numpy 오류.

**원인**: bat에 `python -c "..."` 라고만 쓰면 PATH 순서상 32-bit 파이썬이 먼저 잡힘.

**해결**:
- bat에서 **반드시 `py -3.11` 사용** (Python Launcher가 64-bit 우선)
- 32-bit 필요한 Collector 는 **`py -3.11-32`**

---

### 4) `ModuleNotFoundError: No module named 'alerts'` 등

**증상**: Scheduler 창에서 import 에러.

**원인**: `python scripts/start_scheduler.py` 처럼 scripts/ 경로로 직접 실행하면 sys.path[0] = scripts/ 가 되어 프로젝트 루트(alerts/)를 못 찾음.

**해결** (이미 적용됨):
- 각 `scripts/start_*.py` 상단에 다음 코드가 있어야 함:
  ```python
  import sys
  from pathlib import Path
  _ROOT = Path(__file__).resolve().parent.parent
  if str(_ROOT) not in sys.path:
      sys.path.insert(0, str(_ROOT))
  ```
- 이게 빠졌으면 각 진입 스크립트에 추가

---

### 5) 바탕화면에 중복 `stock` 폴더가 다시 나타남

**증상**: `Desktop\stock` 또는 `OneDrive\Desktop\stock` 재생성.

**원인**: 과거 설치/백업 과정에서 남은 잔재, 또는 OneDrive 동기화.

**해결**:
```
powershell -ExecutionPolicy Bypass -File C:\stock\scripts\delete_desktop_stock.ps1
```
휴지통으로 안전하게 이동 (복구 가능).

**예방**: 새 bat/python 스크립트는 **절대 바탕화면에 만들지 않기**. C:\stock 아래에서만 작업.

---

### 6) 텔레그램 알림에 "AI 의견: API 키 미설정"

**증상**: 매매 신호에 "AI 의견: API 키 미설정" 이 항상 붙음.

**원인**: `.env`에 `ANTHROPIC_API_KEY` 가 없어서 AI 보조 판단이 꺼져있음.

**처리**: **무시해도 무방**. 규칙 기반 전략은 정상 동작.
- 켜려면: Anthropic 유료 API 키 발급 후 `.env`에 `ANTHROPIC_API_KEY=sk-...` 추가
- **MOCK 검증 기간엔 그냥 꺼둔 상태로 진행 권장**

---

### 7) 바로가기 더블클릭해도 창이 안 뜸 / 순식간에 닫힘

**증상**: `Stock Start` 바로가기를 눌렀는데 아무 반응 없거나 창이 0.5초 뒤 사라짐.

**원인 후보**:
- (A) `C:\stock\run_all.bat` 가 삭제/이동됨
- (B) Python 설치 안 됨 또는 경로 문제
- (C) `py` launcher 미설치

**해결**:
1. `cmd` 창을 직접 열고 `C:\stock\run_all.bat` 입력 → 에러 메시지 확인
2. `py -0` 실행 → 설치된 python 목록 확인 (3.11, 3.11-32 둘 다 있어야 함)
3. 없으면 python.org 에서 재설치

---

### 8) cmd 창이 너무 많이 떠있음 (3개 이상)

**증상**: 작업관리자에 cmd.exe, python.exe 가 5개 이상.

**원인**: 이전 실행 잔재 + 새 실행 중복.

**해결** (전체 재시작):
1. 모든 python, cmd 창을 X로 닫기 (또는 작업관리자 종료)
2. `C:\stock\run_all.bat` 더블클릭
3. **정상 구성**: python 2개(Collector 32-bit + Scheduler 64-bit), cmd 3개(Collector + Scheduler + run_all)

---

## 🛠️ 유틸리티 스크립트

| 스크립트 | 용도 |
|---------|------|
| `scripts/health_check.py` | 환경 자가 진단 (실행 전 점검) |
| `scripts/delete_desktop_stock.ps1` | 바탕화면 중복 stock 폴더 휴지통 이동 |
| `scripts/create_shortcuts.vbs` | 바탕화면 바로가기 3종 재생성 |
| `scripts/check_python_cwd.ps1` | 실행 중인 python의 경로/비트 확인 |

---

## 📞 어떻게 해도 안 될 때

1. `py -3.11 scripts\health_check.py` 결과를 스크린샷
2. 문제 있는 창(Collector/Scheduler)의 마지막 10~20줄을 스크린샷
3. Claude 에게 전달 ("이런 증상이고, health_check 결과는 이래")

위 정보 3개만 있으면 거의 다 진단 가능합니다.

---

## 📅 변경 이력

- **2026-04-20**: Desktop\stock 중복 폴더 + bat 이스케이프 + 32-bit 혼선으로 MOCK 런칭 당일 장애. 본 문서와 진입 스크립트 분리로 재발 차단.
