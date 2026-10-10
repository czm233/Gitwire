#!/usr/bin/env python3
"""flake 检测器:连跑 N 轮 e2e,按用例聚合结果,找出"时绿时红"的不稳定用例。

不稳定用例(flaky)比纯失败更危险:它让人对红灯脱敏,真回归反而被当作抖动忽略。
判定:同一用例在不同轮次出现 passed 与 failed/timed-out 混杂 → flaky;
轮轮 failed → 稳定失败(不是 flaky,是 bug);轮轮 passed → 稳定。

用法:
    uv run python scripts/flake_check.py               # 默认 5 轮
    uv run python scripts/flake_check.py --rounds 3
    uv run python scripts/flake_check.py --keep-going  # 有失败也跑完全部轮次再汇总
输出:data/flake-report.json + 终端摘要;发现 flaky 时退出码 1(CI 可拦截)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
REPORT = FRONTEND / '.e2e' / 'report.json'
OUT = ROOT / 'data' / 'flake-report.json'


def run_round(index: int) -> dict[str, str]:
    """跑一轮全量 e2e,返回 {用例标题: 本轮状态}。"""
    print(f'—— 第 {index} 轮 ——', flush=True)
    result = subprocess.run(
        ['uv', 'run', 'python', 'scripts/e2e_stack.py', 'run'],
        cwd=ROOT, capture_output=True, text=True)
    if not REPORT.exists():
        raise SystemExit(f'第 {index} 轮没有产出 report.json:\n{result.stdout[-800:]}')
    data = json.loads(REPORT.read_text())
    outcomes: dict[str, str] = {}
    for record in data.get('run', {}).get('results', []):
        title = ' '.join(record.get('titlePath') or [])
        outcomes[title] = record.get('status', 'unknown')
    print(f'   {len(outcomes)} 条用例,{sum(1 for s in outcomes.values() if s == "passed")} 通过', flush=True)
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--keep-going', action='store_true',
                        help='有失败轮也继续跑完(默认遇到稳定失败立即停止)')
    args = parser.parse_args()

    started = time.time()
    rounds: list[dict[str, str]] = []
    for index in range(1, args.rounds + 1):
        rounds.append(run_round(index))
        # 稳定失败(全轮同一条 failed)提前止损
        if not args.keep_going and index >= 2:
            stable_fail = {
                title for title, states in aggregate(rounds).items()
                if states['flaky'] is False and states['status'] == 'failed'
            }
            if stable_fail:
                print(f'\n检测到稳定失败(非抖动,是 bug),提前停止:{sorted(stable_fail)}')
                break

    summary = aggregate(rounds)
    report = {
        'rounds': len(rounds),
        'at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'total': len(summary),
        'stable_pass': sum(1 for s in summary.values() if s['status'] == 'passed' and not s['flaky']),
        'stable_fail': sum(1 for s in summary.values() if s['status'] == 'failed' and not s['flaky']),
        'flaky': sum(1 for s in summary.values() if s['flaky']),
        'duration_s': round(time.time() - started, 1),
        'cases': summary,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f"\n=== flake 检测({report['rounds']} 轮,{report['duration_s']}s)===")
    print(f"稳定通过 {report['stable_pass']} · 稳定失败 {report['stable_fail']} · 抖动 {report['flaky']}")
    for title, states in sorted(summary.items()):
        if states['flaky']:
            print(f"  ⚠ 抖动 {title}  轮次: {' '.join(states['rounds'])}")
    print(f'报告:{OUT}')
    sys.exit(1 if report['flaky'] or report['stable_fail'] else 0)


def aggregate(rounds: list[dict[str, str]]) -> dict[str, dict]:
    """把 N 轮结果按用例聚合:状态序列 + flaky 判定 + 平均耗时无(耗时由趋势功能看)。"""
    all_states: dict[str, list[str]] = defaultdict(list)
    for outcomes in rounds:
        for title, state in outcomes.items():
            all_states[title].append(state)
    summary = {}
    for title, states in all_states.items():
        seen = set(states)
        # 注意显式 bool():and 表达式会返回集合本身而非布尔
        flaky = bool('passed' in seen and (seen & {'failed', 'timed-out'}))
        final = 'failed' if seen & {'failed', 'timed-out'} else 'passed'
        summary[title] = {'rounds': states, 'flaky': flaky, 'status': final}
    return summary


if __name__ == '__main__':
    main()
