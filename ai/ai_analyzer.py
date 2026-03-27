"""AI 주식 분석기 — Claude Sonnet/Haiku 기반"""
import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")


class AIAnalyzer:
    def __init__(self, model: str = "claude-sonnet-4-6"):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = model
        if self.api_key:
            logger.info(f"AI Analyzer 초기화 완료 (기본 모델: {model})")
        else:
            logger.warning("ANTHROPIC_API_KEY 미설정 — AI 분석 비활성화")

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _call_claude(self, prompt: str, max_tokens: int = 300) -> str:
        """Claude API 단일 호출. 실패 시 예외 전파."""
        import anthropic  # 런타임 임포트 — API 키 없을 때 의존성 불필요
        client = anthropic.Anthropic(api_key=self.api_key)
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()

    # ------------------------------------------------------------------
    # 공개 메서드
    # ------------------------------------------------------------------

    def quick_signal_alert(
        self,
        ticker: str,
        name: str,
        price: float,
        change_rate: float,
        signal_reasons: list[str],
        rsi: float,
        macd_cross: str | None,
        vol_ratio: float,
        recent_candles: list | None = None,
        orderbook: dict | None = None,
        exec_strength: float = 0,
        warnings: list[str] | None = None,
    ) -> dict:
        """매수 신호 발생 시 AI 빠른 판단.

        Returns:
            {"decision": "매수"|"관망"|"매도", "text": str}
        """
        if not self.api_key:
            return {"decision": "관망", "text": "API 키 미설정"}

        candle_info = ""
        if recent_candles:
            candle_info = f"\n최근 캔들: {json.dumps(recent_candles, ensure_ascii=False)}"

        orderbook_info = ""
        if orderbook:
            orderbook_info = f"\n호가창: {json.dumps(orderbook, ensure_ascii=False)}"

        prompt = f"""당신은 한국 주식 단타 자동매매 AI입니다.
이 신호는 이미 RSI, MACD, 볼린저밴드, 이동평균, 거래량 등 10개 기술적 조건에서 STRONG(6점 이상) 판정을 받았습니다.
기술적 분석은 이미 완료되었으므로, 당신은 치명적 리스크가 있는 경우에만 [관망]을 판단하세요.

종목: {name} ({ticker})
현재가: {price:,}원 ({change_rate:+.1f}%)
RSI: {rsi:.1f}
MACD: {macd_cross or '없음'}
거래량 배수: {vol_ratio:.1f}x
체결강도: {exec_strength:.1f}
매수 신호 사유: {', '.join(signal_reasons)}
경고: {', '.join(warnings or [])}{candle_info}{orderbook_info}

판단 기준:
- [매수]: 신호가 유효하고 치명적 리스크 없음 (기본 판단)
- [관망]: 급락 직후 반등 미확인, 거래량 극히 부족, 상한가 근접 등 명백한 위험 시에만
- [매도]: 하락 추세 명확

2줄 이내로 핵심만 분석하고, 반드시 첫 줄에 [매수], [관망], [매도] 중 하나를 표시하세요."""

        try:
            text = self._call_claude(prompt, max_tokens=300)
            decision = "관망"
            if "[매수]" in text:
                decision = "매수"
            elif "[매도]" in text:
                decision = "매도"
            return {"decision": decision, "text": text}
        except Exception as e:
            logger.error(f"AI quick_signal_alert 실패: {e}")
            return {"decision": "관망", "text": f"AI 분석 실패: {e}"}

    def daily_report(self, portfolio_data: dict, market_data: dict) -> str:
        """일일 마감 리포트 생성.

        Args:
            portfolio_data: 보유 종목·손익 정보
            market_data:    당일 시장 요약 데이터

        Returns:
            500자 이내 리포트 문자열
        """
        if not self.api_key:
            return "API 키 미설정"

        prompt = f"""한국 주식 일일 마감 리포트를 작성해주세요.
포트폴리오: {json.dumps(portfolio_data, ensure_ascii=False)}
시장 데이터: {json.dumps(market_data, ensure_ascii=False)}
500자 이내로 핵심만 간결하게."""

        try:
            return self._call_claude(prompt, max_tokens=1000)
        except Exception as e:
            logger.error(f"일일 리포트 생성 실패: {e}")
            return f"리포트 생성 실패: {e}"
