from dataclasses import dataclass
import httpx
from redis.asyncio import Redis
from app.config import Settings
from app.hosted.security import SecretBox
from app.hosted.store import engine_for


@dataclass
class Runtime:
    settings: Settings
    engine: object
    box: SecretBox
    http: httpx.AsyncClient
    redis: Redis | None

    @classmethod
    def create(cls, settings: Settings):
        return cls(settings, engine_for(settings), SecretBox(settings.encryption_key),
                   httpx.AsyncClient(timeout=30, follow_redirects=False),
                   Redis.from_url(settings.redis_url, decode_responses=True,
                                  socket_connect_timeout=2, socket_timeout=2) if settings.redis_url else None)

    async def close(self):
        await self.http.aclose()
        if self.redis:
            await self.redis.aclose()
        self.engine.dispose()
