import { test, expect } from 'e2e';
import { llm, ensureAgentModel } from './llm-env';

/**
 * agent 步骤演示 —— AI 负责探索操作路径,结果仍由确定性断言裁决。
 * 未配置模型时静态跳过;配置了但能力不达标(纯文本模型、缺图片输入或工具调用)
 * 时,运行时跳过并给出后果与配置方法(见 llm-env.ts 的探测)。
 * 冷跑:agent.act 调模型学会路径并录制缓存;之后回放零模型调用。
 * 成本注意:agent.assert 每次运行都调模型,本文件只用 act + expect。
 */
test('agent 演示:AI 自己切换候选来源,断言仍由代码裁决', { tags: ['smoke'],  session: 'pilot', skip: !llm && '未配置模型 key' }, async ({ app, agent, browser }) => {
  // 先探测模型能力(图片输入 + 工具调用),不合格则带着原因跳过本条
  const check = await ensureAgentModel();
  // 探测失败:跳过并显示原因(后果 + 配置方法写在原因里)
  if (!check.ok) test.skip(true, check.reason);
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 操作路径交给 AI:怎么找到下拉、怎么选中,由 agent 对着真实页面自己决定
  await agent.act('把仓库来源筛选切换为「我的公开仓库」');
  // 以下结果判定不交给 AI —— 必须列出 3 条公开仓库
  await expect(browser.locator('table.tbl tbody tr')).toHaveCount(3);
  // 断言:Star 时间列头消失(公开仓库不显示该列)
  await expect(browser.locator('th[title*="Star 的时间"]')).toHaveCount(0);
});
