#!/usr/bin/env python3
"""Gitwire 统一检查入口:本机与 CI 调用同一套命令,不复制两套检查逻辑。

用法:
    uv run python scripts/check.py            # = all,提交前的完整门禁
    uv run python scripts/check.py backend    # pytest(业务规则 + 接口/集成,假 GitHub/LLM,隔离)
    uv run python scripts/check.py frontend   # 前端单元测试(node:test)
    uv run python scripts/check.py build      # tsc 类型检查 + vite 构建
    uv run python scripts/check.py e2e        # 浏览器端到端(隔离栈:sqlite+种子数据,自动起停)
    uv run python scripts/check.py smoke      # 提交前快速门禁:pytest + e2e 冒烟子集(--tag smoke)
    GITWIRE_COV=1 uv run python scripts/check.py backend  # 带覆盖率与防倒退线(基线 80%)

验收环境生命周期(启动/状态/停止共享设施)由 scripts/acceptance.py 管理;
e2e 隔离栈由 scripts/e2e_stack.py 管理,二者互不影响。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'


def step(name: str, args: list[str], cwd: Path = ROOT) -> bool:
    print(f'\n=== {name}: {" ".join(args)} ===', flush=True)
    return subprocess.run(args, cwd=cwd).returncode == 0


def backend() -> bool:
    # GITWIRE_COV=1 启用覆盖率与防倒退线(基线 80%,线设 79 留余量)
    args = ['uv', 'run', 'pytest', 'tests/', '-q']
    if os.environ.get('GITWIRE_COV') == '1':
        args += ['--cov=app', '--cov-report=term', '--cov-fail-under=79']
    return step('backend pytest', args)


def frontend() -> bool:
    return step('frontend unit tests', ['npm', 'run', 'test'], FRONTEND)


def build() -> bool:
    return step('frontend build (tsc + vite)', ['npm', 'run', 'build'], FRONTEND)


def e2e() -> bool:
    return step('browser e2e (isolated stack)', ['uv', 'run', 'python', 'scripts/e2e_stack.py', 'run'])


def e2e_smoke() -> bool:
    return step('browser e2e smoke subset', ['uv', 'run', 'python', 'scripts/e2e_stack.py', 'run', '--', '--tag', 'smoke'])


STEPS = {'backend': backend, 'frontend': frontend, 'build': build, 'e2e': e2e, 'smoke': e2e_smoke}


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if action not in {*STEPS, 'all', 'smoke'}:
        raise SystemExit(__doc__)
    if action == 'smoke':
        results = {'backend': backend(), 'smoke': e2e_smoke()}
    else:
        results: dict[str, bool] = {}
        for name in (['backend', 'frontend', 'build', 'e2e'] if action == 'all' else [action]):
            results[name] = STEPS[name]()
    print('\n=== check 结果 ===')
    for name, ok in results.items():
        print(f'{"✓" if ok else "×"} {name}')
    sys.exit(0 if all(results.values()) else 1)


if __name__ == '__main__':
    main()
