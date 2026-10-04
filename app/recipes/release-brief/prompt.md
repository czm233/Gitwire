# Release 简报（release-brief）

- 目标仓库：{{REPO}}
- 版本：{{TAG}}（{{RELEASE_NAME}}，发布于 {{PUBLISHED_AT}}）
- 简报日期：{{DATE}}

## 官方 Release Notes

{{BODY}}

## 近期提交（供交叉核对）

{{RECENT}}

# 任务

写这个版本的情报简报：这个版本真正改了什么、对使用方有什么影响、升级要注意什么。
官方 notes 常常漏掉关键变化，结合提交列表交叉核对；notes 与提交对不上的地方要点破。

# 输出契约（违反即作废重来）

1. 输出简报文件（文件名必须精确）：

```
<<<FILE:releases/{{TAG}}.md>>>
（完整 markdown：BLUF 一句话 → 升级建议 → 变更明细 → 与 notes 的出入）
```

2. 警报行（新 release 总是输出一行）：

```
<<<ALERT:release|{{TAG}} 发布|一句话版本要点>>>
```

3. 最后一行摘要：

```
<<<META:summary>>>一句话版本结论（≤60字）
```

- 中文写作；不确定的标「未核实」；不得编造
