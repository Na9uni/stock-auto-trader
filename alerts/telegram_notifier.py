"""텔레그램 알림 발송"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")

_MAX_MSG_LEN = 4096

# Telegram bot token 패턴 (숫자:영숫자_-) — 로그 유출 방지용 마스킹
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def mask_bot_token(text) -> str:
    """URL/메시지/예외 문자열에 포함된 bot token 마스킹.

    requests 예외, HTTP 응답 바디 등 외부 소스에서 URL이 노출될 때 토큰 보호.
    """
    return _BOT_TOKEN_RE.sub("bot***MASKED***", str(text))


class TelegramNotifier:
    def __init__(self) -> None:
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.default_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.admin_id = os.getenv("TELEGRAM_ADMIN_ID", "")
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, text: str, chat_id: str = "") -> bool:
        """단일 사용자에게 메시지 전송.

        chat_id가 없으면 default_chat_id 사용.
        4096자 초과 시 분할 전송.
        실패 시 False 반환 및 로그 기록.
        """
        target = chat_id or self.default_chat_id
        if not target:
            logger.warning("send_message: chat_id가 설정되지 않았습니다.")
            return False
        if not self.bot_token:
            logger.warning("send_message: TELEGRAM_BOT_TOKEN이 설정되지 않았습니다.")
            return False

        chunks = _split_text(text, _MAX_MSG_LEN)
        success = True
        for chunk in chunks:
            ok = self._post_message(target, chunk)
            if not ok:
                success = False
        return success

    def send_to_users(self, user_ids: list[str], text: str) -> bool:
        """여러 사용자에게 전송.

        user_ids가 비어 있으면 get_all_chat_ids()로 전체 전송 (broadcast).
        """
        targets = user_ids if user_ids else self.get_all_chat_ids()
        if not targets:
            logger.warning("send_to_users: 전송 대상 chat_id가 없습니다.")
            return False

        results = [self.send_message(text, cid) for cid in targets]
        return all(results)

    def broadcast(self, text: str) -> bool:
        """등록된 모든 사용자에게 전송."""
        return self.send_to_users([], text)

    def get_all_chat_ids(self) -> list[str]:
        """TELEGRAM_ALLOWED_IDS 환경변수에서 모든 chat_id 반환."""
        allowed = os.getenv("TELEGRAM_ALLOWED_IDS", "")
        return [x.strip() for x in allowed.split(",") if x.strip()]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_message(self, chat_id: str, text: str) -> bool:
        """텔레그램 sendMessage API 호출.

        1차: HTML parse_mode, 2차: plain text fallback (< 문자 등 HTML 파싱 실패 대비),
        3차: 3초 대기 후 plain text 재시도.
        """
        import time as _time

        url = f"{self.base_url}/sendMessage"

        def _try(use_html: bool) -> tuple[bool, int, str]:
            payload: dict = {"chat_id": chat_id, "text": text}
            if use_html:
                payload["parse_mode"] = "HTML"
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True, resp.status_code, ""
                return False, resp.status_code, resp.text[:200]
            except requests.RequestException as exc:
                return False, 0, mask_bot_token(exc)

        # 1차: HTML 모드
        ok, status, body = _try(use_html=True)
        if ok:
            return True

        # 2차: 400 parse 에러는 즉시 plain text로 fallback
        if status == 400 and "parse entities" in body:
            ok, status2, body2 = _try(use_html=False)
            if ok:
                logger.info("텔레그램 HTML 파싱 실패 → plain text로 재발송 성공 chat_id=%s", chat_id)
                return True
            logger.error(
                "텔레그램 발송 실패(plain fallback) chat_id=%s status=%s body=%s",
                chat_id, status2, body2,
            )
            return False

        logger.error(
            "텔레그램 발송 실패 chat_id=%s status=%s body=%s (attempt=1)",
            chat_id, status, mask_bot_token(body),
        )

        # 3차: 3초 대기 후 plain text로 재시도 (네트워크 일시 오류 대비)
        _time.sleep(3)
        ok, status3, body3 = _try(use_html=False)
        if ok:
            return True
        logger.error(
            "텔레그램 발송 실패 chat_id=%s status=%s body=%s (attempt=2)",
            chat_id, status3, body3,
        )
        return False


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _split_text(text: str, max_len: int) -> list[str]:
    """텍스트를 max_len 이하 청크로 분할."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        chunks.append(text[:max_len])
        text = text[max_len:]
    return chunks
