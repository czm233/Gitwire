#!/usr/bin/env python3
"""答案卡(answer card)= 人类批准过的测试断言,是 Gitwire 回归体系的合同层。

受保护路径(frontend/e2e/**、tests/**、scripts/e2e_stack.py 的种子契约)任何改动,
必须先由人执行 `approve` 签发一次性令牌(15 分钟有效),否则 pre-commit 钩子拒绝提交。

用法:
    uv run python scripts/answer_card.py approve "断言X从『全部』改为『可上手』:产品决议 #42"
    uv run python scripts/answer_card.py status
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = ROOT / 'data' / 'answer-card-approval.json'
VALID_MINUTES = 15

PROTECTED = ('frontend/e2e/', 'tests/', 'scripts/e2e_stack.py')


def approve(message: str) -> None:
    if not message.strip():
        raise SystemExit('必须写清批准原因,例如:approve "默认分组改为可上手:产品决议"')
    APPROVAL.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    APPROVAL.write_text(json.dumps({'message': message.strip(), 'approved_at': now,
                                    'expires_at': now + VALID_MINUTES * 60}, ensure_ascii=False, indent=2))
    print(f'已批准:{message.strip()}')
    print(f'令牌 {VALID_MINUTES} 分钟内有效;建议提交信息附一行「答案卡变更:{message.strip()}」')


def active() -> dict | None:
    if not APPROVAL.exists():
        return None
    try:
        data = json.loads(APPROVAL.read_text())
    except json.JSONDecodeError:
        return None
    return data if data.get('expires_at', 0) > time.time() else None


def status() -> None:
    data = active()
    if data:
        left = int((data['expires_at'] - time.time()) / 60)
        print(f'批准生效中(剩 {left} 分钟):{data["message"]}')
    else:
        print('无生效中的批准;受保护路径的提交会被 pre-commit 拒绝')


def main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == 'approve':
        approve(' '.join(sys.argv[2:]))
    elif len(sys.argv) >= 2 and sys.argv[1] == 'status':
        status()
    else:
        raise SystemExit(__doc__)


if __name__ == '__main__':
    main()
