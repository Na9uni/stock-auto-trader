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

        # 데이터를 사람 말로 번역
        import math
        if math.isnan(vol_ratio) or vol_ratio == 0:
            vol_desc = "거래가 거의 없음"
        elif vol_ratio < 0.5:
            vol_desc = "평소보다 거래 적음"
        elif vol_ratio < 1.5:
            vol_desc = "평소 수준"
        elif vol_ratio < 3.0:
            vol_desc = "평소보다 거래 많음"
        else:
            vol_desc = "거래 폭발적"

        if exec_strength == 0:
            exec_desc = "사고파는 사람 데이터 없음"
        elif exec_strength < 80:
            exec_desc = "파는 사람이 더 많음"
        elif exec_strength < 120:
            exec_desc = "사고파는 힘이 비슷함"
        else:
            exec_desc = "사려는 사람이 더 많음"

        prompt = f"""너는 주식 초보에게 사도 될지 말해주는 도우미야.
이 종목은 자동 분석에서 "살 만하다" 점수를 받았어. 근데 진짜 사도 괜찮은지 한번 더 확인해줘.

종목: {name}
지금 가격: {price:,}원 (오늘 {change_rate:+.1f}%)
거래 상황: {vol_desc}
매수세: {exec_desc}
신호 이유: {', '.join(signal_reasons)}
주의사항: {', '.join(warnings or ['없음'])}

[매수] = 사도 됨, [관망] = 지금은 기다려, [매도] = 팔아야 함

규칙:
- 첫 줄에 [매수], [관망], [매도] 중 하나만
- 둘째 줄에 이유를 한 문장으로. 초등학생도 알아듣게 쉽게
- 전문 용어 절대 쓰지 마"""

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
