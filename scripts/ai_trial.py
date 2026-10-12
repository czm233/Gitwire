#!/usr/bin/env python3
"""AI 试用官:让 AI 以真实用户姿态走查产品核心旅程,输出体验报告。

与确定性回归的分工:回归测试验证"已知行为没坏"(锁);试用官探索
"功能好不好用、符不符合产品意图"(眼睛和嘴);发现经人在驾驶舱/报告里裁决(裁判席)。

每张使命卡跑一次 `npx e2e explore`(无断言自由走查,AI 自己规划步骤、
记录发现 ⚑issue/⚑warning,附复现步骤);全部走完汇总成 Markdown 报告。

用法:
    uv run python scripts/ai_trial.py            # 全部四张使命卡
    uv run python scripts/ai_trial.py --only 1    # 只跑第 1 张(省 token)
    uv run python scripts/ai_trial.py --only 1,2 --max-steps 10
产出:data/ai-trial-report.md(给人审;含每卡的原始走查输出与发现)
注意:每张卡都是真实模型调用(默认 10 步预算);隔离栈数据是种子的 pilot/writer 世界。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
REPORT = ROOT / 'data' / 'ai-trial-report.md'

# 使命卡:按产品核心旅程设计(用户原话:看到 issue→筛选→导入关注→监控→
# 找到适合自己水平的 issue 介入)。姿态设定让 AI 站在目标用户一边挑剔。
CHARTERS = [
    {
        'id': 1,
        'title': '新用户:找到我能解决的 issue',
        'session': None,  # 访客视角:产品主张"无需登录即可查询公开项目"
        'goal': (
            '你是想参与开源的程序员,第一次打开这个网站。你的目标:找到一个适合你水平去解决的 issue。'
            '浏览首页、理解 Issue 雷达的分组和难度含义、尝试用筛选缩小范围。'
            '(站内已索引的公开项目:pilot/alpha、pilot/beta;陌生的真实仓库可能还在采集中。) '
            '以挑剔的新用户视角报告:哪里让你困惑?哪个词看不懂?信息够不够你判断"这个我能不能做"?'
        ),
    },
    {
        'id': 2,
        'title': '导入关注:把项目纳入监控',
        'session': 'pilot',
        'goal': (
            '你是已登录用户(pilot),想把一个关心的公开项目导入监控持续跟踪。'
            '从候选仓库或添加项目入口完成导入,检查默认监控配方是否符合预期。'
            '报告:导入流程有几步?哪里容易迷失?导入后你怎么确认"它开始为我工作了"?'
        ),
    },
    {
        'id': 3,
        'title': '回头客:日常信息是否足以决策',
        'session': 'pilot',
        'goal': (
            '你是每天来看一眼的用户。查看警报、每日晨报、运行历史,回答一个真实问题:'
            '"今天我该介入哪个 issue?"信息够吗?缺什么你会想要(比如对比、收藏、提醒规则)?'
            '把界面不便利之处一条条列出来。'
        ),
    },
    {
        'id': 4,
        'title': '竞品视角:评审与建议',
        'session': 'pilot',
        'goal': (
            '你是用过 GitHub Issues、Good First Issue、OSS Insight 的资深贡献者,来评审这个产品。'
            '核心场景:帮用户找到"适合自己水平能解决"的开源 issue 并参与。'
            '给出:1) 三个做得好的点;2) 三个不如竞品的点(具体到功能);3) 三个你会优先做的改进建议。'
        ),
    },
]


def run_charter(charter: dict, max_steps: int) -> subprocess.CompletedProcess:
    args = ['npx', 'e2e', 'explore', charter['goal'], '--max-steps', str(max_steps),
            '--timeout', '300000']
    if charter['session']:
        args += ['--session', charter['session']]
    print(f"▶ 使命 {charter['id']}:{charter['title']}", flush=True)
    return subprocess.run(args, cwd=FRONTEND, text=True, capture_output=True)


def extract_assessment(output: str) -> str:
    """提取走查结束后的 Assessment 评语(AI 试用官的核心结论)。"""
    match = re.search(r'Assessment\s*\n(.*?)(?:\n\s*Findings|\n\s*Steps)', output, re.S)
    return match.group(1).strip()[:2000] if match else ''


def extract_findings(output: str) -> list[str]:
    """从走查输出中抽取发现行(⚑ 开头)与其后一两行证据。"""
    lines = output.splitlines()
    hits = []
    for index, line in enumerate(lines):
        if '⚑' in line:
            context = ' / '.join(x.strip() for x in lines[index:index + 2] if x.strip())
            hits.append(context[:300])
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', default='', help='只跑指定使命卡,如 1 或 1,3')
    parser.add_argument('--max-steps', type=int, default=10)
    args = parser.parse_args()
    only = {int(x) for x in re.findall(r'\d+', args.only)} if args.only else None

    print('拉起 e2e 隔离栈(种子世界,不碰真实数据)…', flush=True)
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'up'],
                   cwd=ROOT, capture_output=True, check=True)

    sections = []
    try:
        for charter in CHARTERS:
            if only and charter['id'] not in only:
                continue
            started = time.time()
            result = run_charter(charter, args.max_steps)
            duration = round(time.time() - started)
            findings = extract_findings(result.stdout)
            sections.append({
                **charter,
                'duration': duration,
                'exit': result.returncode,
                'findings': findings,
                'assessment': extract_assessment(result.stdout),
                'raw': result.stdout[-4000:],
            })
            mark = '通过' if result.returncode == 0 else '有发现(issue 级)'
            print(f"  {mark},耗时 {duration}s,发现 {len(findings)} 条", flush=True)
    finally:
        subprocess.run([sys.executable, str(ROOT / 'scripts' / 'e2e_stack.py'), 'down'],
                       cwd=ROOT, capture_output=True)

    # 汇总报告:发现清单置顶供人审,每卡原始输出留档。
    # 单卡运行时与既有报告合并(只替换本次跑过的卡),历史卡的结果不丢。
    previous = {}
    if REPORT.exists():
        for chunk in re.split(r'(?m)(?=^## 使命 )', REPORT.read_text(encoding='utf-8')):
            match = re.match(r'## 使命 (\d+):', chunk)
            if match:
                previous[int(match.group(1))] = chunk.rstrip() + '\n'
    lines = ['# AI 试用官报告 · ' + time.strftime('%Y-%m-%d %H:%M'), '',
             '> 走查在隔离栈(种子数据)进行;⚑ 为 AI 的发现,issue 级已导致该卡非零退出。',
             '> 结论仅供参考,以人审为准;确认的问题应转为回归断言。', '']
    for section in sections:
        lines.append(f"## 使命 {section['id']}:{section['title']}"
                     f"({'登录' if section['session'] else '访客'} · {section['duration']}s · 退出码 {section['exit']})")
        lines.append('')
        if section['findings']:
            lines += [f'- {finding}' for finding in section['findings']]
        else:
            lines.append('- 走查完成,无 ⚑ 级发现')
        if section['assessment']:
            lines += ['', '**试用官评语:**', '', section['assessment']]
        lines += ['', '<details><summary>原始走查输出(尾部)</summary>', '',
                  '```', section['raw'], '```', '', '</details>', '']
    def section_text(section) -> str:
        return '\n'.join(lines_for(section))

    def lines_for(section) -> list[str]:
        return [
            f"## 使命 {section['id']}:{section['title']}"
            f"({'登录' if section['session'] else '访客'} · {section['duration']}s · 退出码 {section['exit']})",
            '',
            *(f'- {finding}' for finding in section['findings'] or ['走查完成,无 ⚑ 级发现']),
            *(['', '**试用官评语:**', '', section['assessment']] if section['assessment'] else []),
            '', '<details><summary>原始走查输出(尾部)</summary>', '',
            '```', section['raw'], '```', '', '</details>', '',
        ]

    ordered_ids = [c['id'] for c in CHARTERS]
    merged = {sid: '\n'.join(lines_for(sec)) for sid, sec in zip([x['id'] for x in sections], sections)}
    merged.update({pid: previous[pid] for pid in previous if pid not in merged})
    body = '\n'.join(merged[sid] for sid in ordered_ids if sid in merged)
    REPORT.write_text('\n'.join(lines) + body, encoding='utf-8')
    print(f'\n报告:{REPORT}')
    sys.exit(1 if any(s['exit'] != 0 for s in sections) else 0)


if __name__ == '__main__':
    main()
