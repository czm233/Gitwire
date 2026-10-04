"""LLM 客户端：双协议。

- openai（默认）：任意 OpenAI 兼容接口（DeepSeek / one-api 网关 / 智谱 paas 口…）
- anthropic：Anthropic Messages 协议（GLM Coding Plan 的 open.bigmodel.cn/api/anthropic、
  Gemini 反代 CLIProxyAPI 等）——协议接口参照 VoiceTutor llm_gateway 的接法
"""

from __future__ import annotations

from openai import AsyncOpenAI


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        protocol: str = "openai",
        max_tokens: int = 16384,
        thinking: str = "",
    ):
        self.model = model
        self.protocol = protocol
        self.max_tokens = max_tokens
        self._thinking = thinking
        if protocol == "anthropic":
            from anthropic import AsyncAnthropic

            self._anthropic = AsyncAnthropic(
                base_url=base_url or None,
                api_key=api_key or "EMPTY",
                timeout=900.0,
                max_retries=1,
            )
            self._openai = None
        else:
            self._anthropic = None
            self._openai = AsyncOpenAI(
                base_url=base_url or None,
                api_key=api_key or "EMPTY",
                timeout=900.0,
                max_retries=1,
            )

    async def chat(self, system: str, user: str, temperature: float = 0.3) -> str:
        if not self.model:
            raise LLMError("LLM_MODEL 未配置（.env）")
        try:
            if self.protocol == "anthropic":
                return await self._chat_anthropic(system, user, temperature)
            return await self._chat_openai(system, user, temperature)
        except LLMError:
            raise
        except Exception as e:  # SDK 异常类型繁多，统一包装
            raise LLMError(f"LLM 调用失败: {e}") from e

    async def aclose(self):
        if self._openai:
            await self._openai.close()
        if self._anthropic:
            await self._anthropic.close()

    async def _chat_openai(self, system: str, user: str, temperature: float) -> str:
        resp = await self._openai.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
        )
        content = resp.choices[0].message.content if resp.choices else ""
        if not content:
            raise LLMError("LLM 返回空内容")
        return content

    async def _chat_anthropic(self, system: str, user: str, temperature: float) -> str:
        # anthropic SDK >= 1.x：temperature 已从 create() 移除；thinking 是一等参数
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            kwargs["system"] = system
        if self._thinking == "disabled":
            # 关思考块：批处理场景省时（GLM anthropic 口）
            kwargs["thinking"] = {"type": "disabled"}
        resp = await self._anthropic.messages.create(**kwargs)
        # thinking 块是独立 content block，只取 text 块，天然不污染输出
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        )
        if not text:
            raise LLMError("LLM 返回空内容（无 text 块）")
        return text
