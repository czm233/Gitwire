"""LLM 双协议客户端测试。"""

from app.llm import LLMClient, LLMError


class _Block:
    def __init__(self, type_: str, text: str = ""):
        self.type = type_
        self.text = text


class _AnthropicResp:
    def __init__(self, blocks):
        self.content = blocks


class _FakeMessages:
    def __init__(self, blocks=None, raise_on_call=None):
        self.blocks = blocks or []
        self.raise_on_call = raise_on_call
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        if self.raise_on_call:
            raise self.raise_on_call
        self.calls.append(kwargs)
        return _AnthropicResp(self.blocks)


async def test_anthropic_protocol_extracts_text_blocks():
    client = LLMClient(
        base_url="http://x", api_key="k", model="glm-4.7", protocol="anthropic"
    )
    fake = _FakeMessages(
        [
            _Block("thinking", "内心独白不该出现在输出里"),
            _Block("text", "正文A"),
            _Block("text", "正文B"),
        ]
    )
    client._anthropic.messages = fake  # type: ignore[attr-defined]
    out = await client.chat("系统", "用户")
    assert out == "正文A正文B"
    kw = fake.calls[0]
    assert kw["system"] == "系统"
    assert kw["messages"] == [{"role": "user", "content": "用户"}]
    assert kw["max_tokens"] == 16384
    assert "temperature" not in kw  # anthropic SDK >=1.x 已移除该参数


async def test_anthropic_thinking_disabled_flag():
    client = LLMClient(
        base_url="http://x", api_key="k", model="m",
        protocol="anthropic", thinking="disabled",
    )
    fake = _FakeMessages([_Block("text", "ok")])
    client._anthropic.messages = fake  # type: ignore[attr-defined]
    await client.chat("s", "u")
    assert fake.calls[0]["thinking"] == {"type": "disabled"}


async def test_anthropic_empty_raises():
    client = LLMClient(base_url="http://x", api_key="k", model="m", protocol="anthropic")
    client._anthropic.messages = _FakeMessages([_Block("thinking", "只有思考没有正文")])  # type: ignore[attr-defined]
    try:
        await client.chat("s", "u")
        raise AssertionError("应当抛 LLMError")
    except LLMError as e:
        assert "无 text 块" in str(e)


async def test_missing_model_raises():
    client = LLMClient(base_url="http://x", api_key="k", model="")
    try:
        await client.chat("s", "u")
        raise AssertionError("应当抛 LLMError")
    except LLMError:
        pass
