---
repo: czm233/CC-Balancer
sha: dfecb1fe47d7770f7bbe3b4b69f682323ec2f3e9
date: 2026-09-24
---

# 核心业务逻辑

**一句话：用户描述自己的工作日与额度消耗习惯，应用逐分钟模拟「每 5 小时一份、每份 100 单位」的额度供给，输出各窗口压力与空窗分钟数，并据此生成一份让外部 AI 配置定时触发脚本的 Prompt。** 以下 5 条流程覆盖全部在用逻辑；代码位置均指 `gitwire-sources/czm233-CC-Balancer/`。

## 1. 设置读取、校验与持久化

```mermaid
flowchart TD
    A[用户修改任一设置项] --> B["update(patch)<br/>src/Planner.tsx:35-41"]
    B --> C{"first 夹取到<br/>[start-285, start] 区间"}
    C --> D{"午休 start < end ?"}
    D -->|否| E["提示: 午休结束需晚于开始"]
    D -->|是| F{"valid(next)<br/>src/utils/planner.ts:5"}
    F -->|"否（工作时段需 05:00–23:00、<br/>跨度≥1h、hours∈[1,5] 等）"| G["提示: 请设置合法同日工作时间"]
    F -->|是| H["persist(next)<br/>src/Planner.tsx:25"]
    H --> I["set(next) 更新 React 状态"]
    H --> J["localStorage.setItem<br/>key: cc-balancer-planner-v1"]
    J -->|"写入失败"| K["saved=false: 浏览器无法保存"]
    J -->|成功| L["saved=true: 方案已保存在此浏览器"]
    M[("页面加载")] --> N["read()<br/>src/Planner.tsx:9"]
    N --> O{"JSON 解析成功<br/>且 valid(s) ?"}
    O -->|是| P["恢复用户设置（hours 上限夹到 5）"]
    O -->|否| Q["回落 defaults<br/>src/utils/planner.ts:3"]
```

所有状态只有这一个 `Settings` 对象（8 个字段：start/end/lunch/lunchStart/lunchEnd/hours/profile/first，src/utils/planner.ts:2），任何 UI 操作最终都汇入 `update()` 单点校验。首次触发时间 `first` 被约束在工作开始前 285 分钟内（= 5h 窗口 300 分钟减去 15 分钟安全余量，src/Planner.tsx:37、planner.ts:5）。

## 2. 逐分钟额度模拟（核心算法 simulate）

```mermaid
flowchart TD
    A["demand[1440]<br/>每分钟需求 = consumptionWeight / (hours×60) × 100<br/>src/utils/planner.ts:16"] --> B{"遍历周期窗口<br/>start 从 first 起，每次 +300<br/>直到超出 end（planner.ts:20）"}
    B --> C{"窗口内逐分钟 t:<br/>requested = demand[t]"}
    C --> D["accepted = min(requested,<br/>剩余额度 100 - used)"]
    D --> E["unmet[t] = requested - accepted"]
    E --> F["shortageMinutes += 未满足比例<br/>(requested-accepted)/requested"]
    F --> C
    C -->|"窗口结束"| G["记录 cycle: start/end/used/need"]
    G --> B
    B -->|"全部窗口结束"| H["汇总: satisfaction = served/total×100<br/>unused = 窗口数×100 - served"]
    I["consumptionWeight<br/>planner.ts:9-14"] --> A
    J{"工作时间内?"} -->|"否（含午休）"| K["权重 0：不消耗"]
    J -->|"morning 忙时且 t < 午休开始<br/>或 afternoon 忙时且 t ≥ 午休结束"| L["权重 2：消耗翻倍"]
    J -->|"其余"| M["权重 1"]
    I --> J
```

关键约定（README.md:26-33 同步声明）：
- **忙时翻倍**：选「上午更忙」则上午每工作 1 分钟烧 2 分钟常规额度（planner.ts:13）；无午休时以 12:00 分界上午/下午（planner.ts:11-12）。
- **每窗 100 单位硬上限**：一个 5h 窗口最多供给 100 单位（planner.ts:24），`hours` 越小需求密度越高。
- **空窗按比例累加**：shortageMinutes 不是简单分钟差，而是逐分钟 `未满足量/需求量` 的累加（planner.ts:27）。
- 窗口序列固定从 `first` 起步长 300 分钟，**不做任何窗口检测或动态调整**——模拟假设「首次有效调用开启窗口」。

## 3. 窗口压力与空窗指标（windowPressure）

```mermaid
flowchart TD
    A["windowPressure(s, first)<br/>src/utils/planner.ts:45-63"] --> B{"遍历每个 5h 窗口"}
    B --> C["逐分钟累计:<br/>workMinutes（在岗分钟）<br/>equivalentMinutes（加权消耗）"]
    C --> D{"weight > 0 分钟:<br/>served = min(remaining, weight)<br/>remaining 从 hours×60 起扣"}
    D --> E["gapMinutes += (weight-served)/weight"]
    E --> C
    C -->|"窗口结束且 workMinutes>0<br/>（纯午休/下班窗口丢弃）"| F["pressure =<br/>(equivalent/(hours×60) - 1) × 100"]
    F --> B
    B -->|结束| G["UI 汇总（src/Planner.tsx:32-34）:<br/>总空窗 = Σ gapMinutes（向上取整显示）<br/>平均压力 = 算术平均"]
    F --> H{"pressure 分类<br/>src/Planner.tsx:10"}
    H -->|"> 0"| I["偏紧（橙 #fb923c）"]
    H -->|"= 0"| J["刚好够用（灰 #cbd5e1）"]
    H -->|"< 0"| K["有余量（绿 #34d399）"]
```

注意 `pressure` 是相对量：`(窗口加权需求/一份额度 - 1)×100`，+100% 表示该窗口需求是可支撑时长的两倍（tests/planner.test.mjs:44 有 +400% 用例）。`recommend()`（planner.ts:34-41，15 分钟步长穷举最优 first）当前**仅被测试调用，UI 未接入**。

## 4. 双时钟可视化（ClockFace）

```mermaid
flowchart LR
    A["Planner 派生数据<br/>busy[] 连续工作段（Planner.tsx:43）<br/>cycles[] 5h 窗口取模 1440（Planner.tsx:44）<br/>lunchSlot 裁剪到工作时段内（Planner.tsx:26-28）"] --> B["ClockFace ×2（period=am / pm）<br/>src/components/ClockFace.tsx:123"]
    B --> C["clipSlotToPeriod 裁剪跨午夜区间<br/>（ClockFace.tsx:76-97）"]
    C --> D["minutesToAngle 映射为时钟角度<br/>（ClockFace.tsx:37-41）"]
    D --> E["arcPath 生成 SVG 弧<br/>（ClockFace.tsx:100-119）"]
    E --> F["内圈: 忙时琥珀 #F59E0B + 午休绿 #34D399"]
    E --> G["外圈: 5H 周期两蓝交替<br/>#0EA5E9 / #3B82F6（Planner.tsx:7）"]
    E --> H["同一窗口跨 AM/PM 两钟<br/>保持同色（按索引取色）"]
```

纯展示组件：AM 钟映射 0–720 分钟、PM 钟映射 720–1440 分钟到 0–360°。组件内保留的拖拽创建忙时代码（onBusySlotsChange）因 prop 未接线而处于禁用状态（详见 architecture.md「已知遗留代码」）。

## 5. 脚本 Prompt 生成与保守避用区间

```mermaid
sequenceDiagram
    participant U as 用户
    participant P as Planner.tsx
    participant G as activationGuard(planner.ts:66-69)
    participant S as scriptPrompt.ts
    participant X as 用户自己的 AI

    U->>P: 点击「生成脚本 Prompt」
    P->>S: scriptPrompt(s, timezone)
    Note over P: timezone 取自浏览器<br/>Intl.DateTimeFormat（Planner.tsx:18）
    S->>G: activationGuard(s.first)
    G-->>S: 避用区间 [first-300, first]<br/>含跨午夜「前一天」标签
    S->>S: simulate 取全部触发时点
    S-->>P: Prompt 全文（中文模板）
    U->>P: 点击「复制 Prompt」
    P-->>U: navigator.clipboard.writeText
    U->>X: 粘贴 Prompt，由 X 配置定时任务
    Note over X: 每节点发一次「say OK」请求<br/>不重试/不补跑/不检测窗口
```

Prompt 固定包含：计划时区（要求对方确认）、全部触发时点、执行日期询问项、三条「先向我确认」（定时工具、API 地址/认证、时区）、以及严格的行为边界——不重试、不补跑、不监控窗口状态（src/utils/scriptPrompt.ts:7-33）。**保守避用区间** `activationGuard` 取首次触发前完整 5 小时（planner.ts:67），用于提示用户暂停使用以免既有窗口挤占新窗口开启（测试 tests/planner.test.mjs:59-64 验证跨午夜标签）。

## 非核心逻辑（文字概述）

- **recommend()（planner.ts:34-41）**：从 `start` 向前以 15 分钟步长穷举到 `start-285`，取 served 最大（平手取窗口数更少）的 first。当前无 UI 调用方，仅被 tests/planner.test.mjs:9-14 断言其最优性，属预留能力。
- **time.ts 遗留几何工具**：generateCycles/calculateDeadZone/calculateMetrics 等描述「周期×忙时」旧模型，重构后无调用方（grep 全仓核实），详见 architecture.md。
- **部署**：main 分支 push 触发 GitHub Actions（pnpm 10 / Node 22）构建并发布 GitHub Pages，无测试门禁（.github/workflows/deploy-pages.yml）。
- **无夜班/跨日/多账号**：README.md:30 明确当前只面向同日白天工作场景。
