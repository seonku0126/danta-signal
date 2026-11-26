"""텔레그램 발송. 토큰이 없거나 dry_run이면 콘솔에만 출력."""
from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False, session=None):
        self.token, self.chat_id = token, chat_id
        if token and not chat_id and not dry_run:
            try:  # 채널 ID를 아직 안 넣었으면 봇이 추가된 채널을 자동으로 찾는다 (최근 24시간 내 이벤트 기준)
                found = self.find_chats(requests.Session())
                self.chat_id = found[0][0] if found else ""
            except Exception:
                pass
        self.dry = dry_run or not (token and self.chat_id)
        self.s = session or requests.Session()
        self.sent: list[str] = []

    def send(self, html: str):
        self.sent.append(html)
        if self.dry:
            print("\n[텔레그램 미리보기]\n" + re.sub(r"</?[bi]>", "", html) + "\n")
            return
        try:
            r = self.s.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.ok:
                log.error("텔레그램 발송 실패 %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.error("텔레그램 발송 오류: %s", e)

    def find_chats(self, s=None) -> list[tuple[str, str]]:
        """봇이 추가된 채널/그룹의 chat_id 찾기 (getUpdates)."""
        r = (s or self.s).get(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=10).json()
        found = {}
        for u in r.get("result", []):
            for key in ("my_chat_member", "channel_post", "message"):
                chat = (u.get(key) or {}).get("chat")
                if chat and chat.get("type") in ("channel", "supergroup", "group"):
                    found[str(chat["id"])] = chat.get("title", "")
        return list(found.items())
