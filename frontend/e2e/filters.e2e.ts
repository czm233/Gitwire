import { test, expect } from 'e2e';

/**
 * 列表页筛选器 —— 答案卡,改动断言需人工批准。
 * 覆盖:运行历史按任务类型/状态筛选并可清除、投递记录按状态筛选。
 * 对应 AGENTS.md:筛选与分页统一组件、筛选提供恢复入口。
 * 注意:运行历史的 GSelect 无 aria-label,按显示的 placeholder 文本定位。
 */

test('运行历史:按任务类型筛选只留该类任务', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开运行历史页
  await app.open('/runs');
  // 找到显示"全部任务"的下拉框(按可见文本定位)并点击
  await screen.getByRole('combobox').filter({ hasText: '全部任务' }).click();
  // 在选项列表里选"更新项目档案"
  await screen.getByRole('option', { name: '更新项目档案' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:只剩"更新项目档案"这一类(种子里 1 条失败的 analyze_repo)
  await expect(rows).toHaveCount(1);
  // 断言:这一行状态是"失败"
  await expect(rows.first()).toContainText('失败');
  // 筛选后出现"清除筛选"按钮,点击恢复
  await screen.getByRole('button', { name: '清除筛选' }).click();
  // 断言:恢复后种子的 scan 结果行仍在(行数是动态的,explore/重试会并发追加同类)
  await expect(rows.filter({ hasText: '本次处理 5 项' }).first()).toBeVisible();
});

test('运行历史:按状态筛选只留失败任务', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开运行历史页
  await app.open('/runs');
  // 找到显示"全部状态"的下拉框并点击
  await screen.getByRole('combobox').filter({ hasText: '全部状态' }).click();
  // 在选项列表里选"执行失败"(状态选项的官方文案)
  await screen.getByRole('option', { name: '执行失败' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:只剩 1 行(种子唯一失败任务:更新项目档案)
  await expect(rows).toHaveCount(1);
  // 断言:这一行是"更新项目档案"
  await expect(rows.first()).toContainText('更新项目档案');
});

test('投递记录:按状态筛选只留失败邮件', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开投递记录页
  await app.open('/deliveries');
  // 找到显示"全部状态"的下拉框并点击
  await screen.getByRole('combobox').filter({ hasText: '全部状态' }).click();
  // 在选项列表里选"发送失败"
  await screen.getByRole('option', { name: '发送失败' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:只剩 1 行(种子只有晨报投递失败)
  await expect(rows).toHaveCount(1);
  // 断言:这一行是晨报邮件
  await expect(rows.first()).toContainText('Gitwire 每日晨报');
  // 点击"清除筛选"恢复
  await screen.getByRole('button', { name: '清除筛选' }).click();
  // 断言:回到 2 条
  await expect(rows).toHaveCount(2);
});
