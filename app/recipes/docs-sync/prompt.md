# 任务参数

- 目标仓库：{{REPO}}
- 模式：{{MODE_DESC}}
- 本次同步：`{{OLD_SHA}}` → `{{NEW_SHA}}`（{{DATE}}）
- changelog 文件名（必须精确）：`{{CHANGELOG_PATH}}`

{{CONTEXT}}

{{EXISTING_SECTION}}

# 输出契约（违反任意一条即整体作废重来）

1. 每个文件用如下块输出，路径必须与要求完全一致，内容是**完整文件**（不是片段、不是占位符）：

```
<<<FILE:tech-stack.md>>>
（此处是完整的 markdown 内容）
```

2. 全部文件输出完毕后，最后输出一行态势板摘要：

```
<<<META:summary>>>一句话中文摘要，结论先行，不超过 80 字
```

3. {{REQUIRED_FILES}}
4. 所有 mermaid 图放在 ```mermaid 代码块中，语法必须合法（节点 id 不能有特殊字符，箭头用 -->）
5. 只依据给定材料，不得编造；不确定的结论显式标注「未核实」
6. 中文写作，BLUF（结论先行）；重要结论标注信源（文件路径或 commit 短 sha）

# 档案写作规范

- **README.md**：档案索引——项目一句话定位、各档案链接、最近同步信息
- **tech-stack.md**：表格化技术栈清单（语言 / 框架 / 构建 / 测试 / 部署），附每个选型的用途一句话
- **architecture.md**：一张 mermaid 架构图（组件与依赖关系）+ 每个组件一段职责说明
- **business-logic.md**：3–6 条核心业务流程，每条一张 mermaid sequenceDiagram 或 flowchart + 简短文字说明；非核心流程用文字带过
- **changelog**：按「新增 / 修复 / 重构 / 破坏性变更」分组，条目带 commit 短 sha 信源；没有变化的组写「无」
- **tripwires.md**（可选）：值得持续追踪的问题、当前判定与依据

现在开始，输出全部文件。
