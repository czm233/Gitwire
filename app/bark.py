"""Bark 推送（iOS）。BARK_URL 未配置时静默跳过——警报仍进台账。"""

from __future__ import annotations

import httpx


class BarkClient:
    def __init__(self, url: str = "", group: str = "Gitwire"):
        self.url = url.rstrip("/")
        self.group = group

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(self, title: str, body: str = "", level: str = "") -> bool:
        """非阻塞容错：推送失败只打日志，绝不影响主流程。返回是否送达。"""
        if not self.enabled:
            return False
        params: dict = {"title": title, "group": self.group}
        if body:
            params["body"] = body
        if level:
            params["level"] = level  # active | timeSensitive | passive
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(self.url, params=params)
                return resp.status_code == 200
        except Exception as e:  # noqa: BLE001
            print(f"[bark] 推送失败: {e}")
            return False
