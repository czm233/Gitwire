---
repo: czm233/CC-Balancer
sha: dfecb1fe47d7770f7bbe3b4b69f682323ec2f3e9
date: 2026-09-24
---

# czm233/CC-Balancer 情报档案

源码仓库：https://github.com/czm233/CC-Balancer ｜ 当前建档 SHA：[`dfecb1f`](https://github.com/czm233/CC-Balancer/commit/dfecb1fe47d7770f7bbe3b4b69f682323ec2f3e9)（2026-09-19）

CC-Balancer 是纯前端「额度规划实验室」：模拟 Coding Plan 的 5 小时额度窗口，帮用户规划首次触发时间并生成交给 AI 配置定时调用的 Prompt。

## 文档索引

- [tech-stack.md](tech-stack.md) —— 技术栈清单：TypeScript + React 19 + Vite 7 + Tailwind v4，零后端零数据库，运行时依赖仅 React。
- [architecture.md](architecture.md) —— 三层架构图（React UI / 纯函数计算 / localStorage），并核实重构遗留的死代码与未接线交互。
- [business-logic.md](business-logic.md) —— 5 条核心流程图：设置校验持久化、逐分钟额度模拟、窗口压力计算、双时钟可视化、Prompt 生成。
- [changelog/2026-09-24-dfecb1f.md](changelog/2026-09-24-dfecb1f.md) —— 首版建档说明：建档依据（浅克隆 @ dfecb1f）与项目现状速览（测试/lint/build 实测通过）。

## 元数据

见 [meta.yml](meta.yml)。下一轮 sync 从 `last_synced: dfecb1fe…` 起做增量 diff。
