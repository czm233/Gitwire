"""开发用 mock LLM 服务器：实现 OpenAI /chat/completions 接口。

不消耗真实 token，用于本地端到端验证 Gitwire 全链路。
按 prompt 标记识别配方：docs-sync（建档/增量）、feature-tripwire、bug-watch、
issue-radar（难度分析 + 口认领检测）、release-brief、每日晨报。

用法：uv run python scripts/mock_llm.py [port]   # 默认 9377
"""

from __future__ import annotations

import re
import sys
import time

import uvicorn
from fastapi import FastAPI, Request

app = FastAPI(title="mock-llm")

CLG_RE = re.compile(r"changelog 文件名（必须精确）：`([^`]+)`")


def build_docs_sync_reply(user: str) -> str:
    m = CLG_RE.search(user)
    clg = m.group(1) if m else "changelog/unknown.md"
    if "首次全量建档" in user:
        return f"""<<<FILE:README.md>>>
# 档案索引（mock）

本项目由 Gitwire 自动建档。

<<<FILE:tech-stack.md>>>
| 项 | 值 | 用途 |
|---|---|---|
| 语言 | 待核 | 主语言 |

<<<FILE:architecture.md>>>
```mermaid
graph TD
    A[入口] --> B[核心]
```

<<<FILE:business-logic.md>>>
核心流程：入口 → 核心 → 输出。

<<<FILE:{clg}>>>
# 初始建档（mock）

- 依据仓库快照建立全套档案

<<<META:summary>>>（mock）完成初始建档
"""
    return f"""<<<FILE:{clg}>>>
# 增量更新（mock）

## 新增
- 示例条目（sha7 未核实）

<<<META:summary>>>（mock）增量更新完成
"""


def build_recipe_reply(user: str) -> str:
    if "Issue 难度分析（issue-radar" in user:
        import re

        nums = re.findall(r"### #(\d+)", user)
        blocks = []
        for n in nums:
            blocks.append(
                f"<<<ISSUE:#{n}|简单|（mock）结论>>>\n- 问题：示例\n- 方案：示例（小时级）"
            )
        return "\n".join(blocks) + "\n"
    if "哨兵警戒（feature-tripwire）" in user:
        return (
            "<<<FILE:tripwires.md>>>\n"
            "# 哨兵清单（mock）\n\n"
            "- T1 示例哨兵：关注中\n\n"
            "<<<ALERT:tripwire|示例哨兵翻转（mock）|判定从关注翻转为已确认>>>\n"
            "<<<META:summary>>>（mock）哨兵巡检完成\n"
        )
    if "缺陷观察（bug-watch）" in user:
        return (
            "<<<FILE:bugwatch.md>>>\n"
            "# 缺陷观察（mock）\n\n## 本批\n- 修复示例缺陷\n\n"
            "<<<ALERT:breaking|示例破坏性变更（mock）|接口签名变化>>>\n"
            "<<<META:summary>>>（mock）缺陷观察完成\n"
        )
    if "口认领检测（issue-radar·claim）" in user:
        return "<<<CLAIM:NONE>>>"
    if "Release 简报（release-brief）" in user:
        m = re.search(r"版本：`?([^（`\n]+)", user)
        tag = m.group(1).strip() if m else "v0"
        return (
            f"<<<FILE:releases/{tag}.md>>>\n# {tag} 简报（mock）\n\n> 要点\n\n"
            f"<<<ALERT:release|{tag} 发布（mock）|版本要点>>>\n"
            f"<<<META:summary>>>（mock）{tag} 已发布\n"
        )
    return build_docs_sync_reply(user)


def build_daily_reply(user: str) -> str:
    return """# 晨报（mock）

> 今日总览：mock 晨报

## 最值得关注
- mock 条目

## 各项目动态
- 见各项目 changelog

## 明日追踪
- 无
"""


async def _completions_impl(request: Request):
    body = await request.json()
    user = ""
    for m in body.get("messages", []):
        if m.get("role") == "user":
            user = m.get("content", "")
    if "每日晨报" in user:
        content = build_daily_reply(user)
    elif any(
        marker in user
        for marker in ("哨兵警戒", "缺陷观察", "Issue 难度分析", "口认领检测", "Release 简报")
    ):
        content = build_recipe_reply(user)
    else:
        content = build_docs_sync_reply(user)
    return {
        "id": "mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", "mock"),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/chat/completions")
async def chat_completions(request: Request):
    return await _completions_impl(request)


@app.post("/v1/chat/completions")
async def chat_completions_v1(request: Request):
    return await _completions_impl(request)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9377
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
