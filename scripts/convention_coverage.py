#!/usr/bin/env python3
"""AGENTS.md 前端规矩 ↔ e2e 用例覆盖对照。

把仓库约定(AGENTS.md 的前端规范)登记成清单,自动对照 frontend/e2e/*.e2e.ts
的用例标题/注释,输出:已上卡 / 未覆盖 两张清单。未覆盖的规矩就是答案卡
下一步该补的条目——规矩没有断言守护,和没有写下来一样。

用法:
    uv run python scripts/convention_coverage.py           # 打印报告
    uv run python scripts/convention_coverage.py --strict  # 有未覆盖时退出码 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
E2E_DIR = ROOT / 'frontend' / 'e2e'
OUT = ROOT / 'data' / 'convention-coverage.json'

# 规矩登记表:(规矩摘要, 匹配用例的正则;正则作用域=用例标题+文件内注释)
# 新增 AGENTS.md 规矩时同步登记,保持"规矩必须有断言"闭环。
REGISTRY: list[tuple[str, str]] = [
    ('`/` 重定向到 /issues,不建总览页', r'根路径重定向'),
    ('Issue 雷达默认分组是「全部」', r'默认分组'),
    ('统计卡「全部」固定最左且默认激活', r'统计条:六张卡'),
    ('页面标题不显示数量', r'不显示数量'),
    ('列表默认最新在前', r'最新在前|最新 Star 在最前'),
    ('单页数据不渲染分页条', r'单页数据不渲染|每页切换为 50'),
    ('每页 10/25/50 可切换', r'每页切换为 10'),
    ('上一页/下一页按位置出现,不用首末页按钮', r'第一页只有下一页'),
    ('Star 时间用 GitHub starred_at 倒序,未获取排最后', r'无时间的排在最后|Star 时间列存在'),
    ('我的公开仓库不显示 Star 时间列', r'不显示 Star 时间列'),
    ('标题下只显示忠实中文翻译,原文变化旧翻译失效', r'过期翻译不显示'),
    ('机会筛选依据放详情「机会判断依据」,不堆标题', r'未入榜」分组只列 #9|判断依据」标签'),
    ('登录和账户入口在右上角,仅图标', r'右上角是登录图标'),
    ('未登录访问个人页:说明登录用途+公开浏览入口', r'公开浏览入口|登录用途'),
    ('访客可免登录查询公开项目(添加项目)', r'添加项目查询'),
    ('确认弹窗统一 Confirm,禁止原生 confirm', r'走确认弹窗'),
    ('下拉筛选统一 GSelect,禁止原生 select', r'getByRole\(.combobox'),
    ('添加监控默认配方「档案同步 + issue 雷达」', r'默认勾选「档案同步'),
    ('请求失败显示错误并提供重试,不当"暂无数据"', r'显示错误提示|失败可重试'),
    ('越权/无效输入如实报错(401/400 路径)', r'无效仓库|无效 token'),
    ('退出登录只退出本站会话', r'退出登录回到访客态'),
    ('设置页展示只读授权徽章', r'只读连接徽章'),
]


def corpus() -> str:
    """全部用例文件的标题+注释拼成语料(注释也是断言意图的书面表达)。"""
    parts = []
    for path in sorted(E2E_DIR.glob('*.e2e.ts')):
        parts.append(path.read_text())
    return '\n'.join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true', help='存在未覆盖规矩时退出码 1')
    args = parser.parse_args()

    text = corpus()
    covered, uncovered = [], []
    for rule, pattern in REGISTRY:
        (covered if re.search(pattern, text) else uncovered).append(rule)

    report = {
        'at': __import__('time').strftime('%Y-%m-%dT%H:%M:%S'),
        'total': len(REGISTRY), 'covered': len(covered), 'uncovered': len(uncovered),
        'covered_rules': covered, 'uncovered_rules': uncovered,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print(f'=== 规矩覆盖对照:{len(covered)}/{len(REGISTRY)} 已上卡 ===')
    for rule in covered:
        print(f'  ✓ {rule}')
    if uncovered:
        print(f'--- 未覆盖({len(uncovered)},答案卡待补)---')
        for rule in uncovered:
            print(f'  ✗ {rule}')
    print(f'报告:{OUT}')
    sys.exit(1 if args.strict and uncovered else 0)


if __name__ == '__main__':
    main()
