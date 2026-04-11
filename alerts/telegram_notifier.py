"""텔레그램 알림 발송"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(ROOT / ".env")
logger = logging.getLogger("stock_analysis")

_MAX_MSG_LEN = 4096


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
        """텔레그램 sendMessage API 호출. 실패 시 3초 후 1회 재시도."""
        import time as _time

        url = f"{self.base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                if resp.status_code == 200 and resp.json().get("ok"):
                    return True
                logger.error(
                    "텔레그램 발송 실패 chat_id=%s status=%s body=%s (attempt=%d)",
                    chat_id,
                    resp.status_code,
                    resp.text[:200],
                    attempt + 1,
                )
                if attempt == 0:
                    _time.sleep(3)
            except requests.RequestException as exc:
                logger.error(
                    "텔레그램 발송 예외 chat_id=%s error=%s (attempt=%d)",
                    chat_id, exc, attempt + 1,
                )
                if attempt == 0:
                    _time.sleep(3)
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
