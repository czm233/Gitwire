import { test, expect } from 'e2e';

const APP_URL = process.env.E2E_APP_URL ?? 'http://127.0.0.1:10249';

// 前置:注入 quitter 会话(退出登录会删服务端会话,用独立账户不影响其他测试)
test.setup('前置:注入会话登录测试账户 quitter', { sessions: ['quitter'] }, async ({ app, screen, browser, session }) => {
  // 打开任意页面建立域名上下文
  await app.open('/');
  // 写入 quitter 的种子会话 cookie
  await browser.setCookies([{ url: APP_URL, name: 'gitwire_session', value: 'e2e-quitter-session' }]);
  // 打开设置页确认登录态
  await app.open('/settings');
  // 断言:已处于登录态(显示登录名)
  await expect(screen.getByText('quitter')).toBeVisible();
  // 保存会话供退出测试复用
  await session.save('quitter');
});

/**
 * 写路径补充 —— 答案卡,改动断言需人工批准。
 * 覆盖:雷达行内追踪切换、未分析 Issue 的分析排队、失败任务重试、
 * 设置页偏好保存(含值变更)与退出登录。全部真实走 API 落库,
 * 会改变状态的用例在结尾把数据改回种子形态。
 */

test('雷达:行内追踪切换并恢复', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页(#11 未被追踪,种子只追踪 #3)
  await app.open('/issues');
  // 找到 #11 所在行
  const row = browser.locator('.host-issues-table tbody tr').filter({ hasText: '#11' });
  // 点击该行的"追踪"按钮(真实 PATCH watched_issues)
  await row.getByRole('button', { name: '追踪' }).click();
  // 断言:按钮变成"取消追踪"(已加入追踪列表)
  await expect(row.getByRole('button', { name: '取消追踪' })).toBeVisible();
  // 再点"取消追踪"恢复种子形态
  await row.getByRole('button', { name: '取消追踪' }).click();
  // 断言:按钮回到"追踪"
  await expect(row.getByRole('button', { name: '追踪' })).toBeVisible();
});

test('雷达:未分析 Issue 可排队分析', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 找到 #5 所在行(种子中唯一"未分析"的 Issue)
  const row = screen.getByRole('row').filter({ hasText: '#5' });
  // 点击该行的"分析"按钮(真实 POST 排队任务)
  await row.getByRole('button', { name: '分析' }).click();
  // 断言:页面出现"分析任务已排队"的状态提示(role=status)
  await expect(screen.getByRole('status')).toContainText('分析任务已排队');
});

test('运行历史:失败任务可重试并产生新等待任务', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开运行历史页
  await app.open('/runs');
  // 找到"更新项目档案"失败行的"详情"按钮并点击打开弹窗
  await browser.locator('table.tbl tbody tr').filter({ hasText: '更新项目档案' })
    .filter({ hasText: '失败' }).getByRole('button', { name: '详情' }).click();
  // 等弹窗加载出"重试任务"按钮(详情是异步查询),再点击(真实 POST /jobs/{id}/retry)
  await screen.getByRole('button', { name: '重试任务' }).waitFor({ state: 'visible' });
  await screen.getByRole('button', { name: '重试任务' }).click();
  // 断言:列表出现一条"等待中"的"更新项目档案"任务(重试产物)
  const retried = browser.locator('table.tbl tbody tr').filter({ hasText: '更新项目档案' })
    .filter({ hasText: '等待中' });
  await expect(retried).toHaveCount(1);
});

test('设置:变更每日汇总时间并保存生效', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开设置页
  await app.open('/settings');
  // 找到显示"22:00"的下拉框(种子免打扰开始 22 点;08:00 会与汇总时间撞名,22 唯一)
  await screen.getByRole('combobox').filter({ hasText: '22:00' }).click();
  // 在选项列表里选"23:00"
  await screen.getByRole('option', { name: '23:00' }).click();
  // 点击"保存提醒设置"(真实 PATCH /preferences)
  await screen.getByRole('button', { name: '保存提醒设置' }).click();
  // 断言:出现"设置已保存"的状态提示
  await expect(screen.getByRole('status')).toContainText('设置已保存');
  // 把免打扰开始改回 22:00(恢复种子形态,不留副作用)
  await screen.getByRole('combobox').filter({ hasText: '23:00' }).click();
  await screen.getByRole('option', { name: '22:00' }).click();
  await screen.getByRole('button', { name: '保存提醒设置' }).click();
});

test('设置:退出登录回到访客态', { session: 'quitter' }, async ({ app, screen }) => {
  // 以 quitter 登录态打开设置页(独立账户,登出不影响其他测试)
  await app.open('/settings');
  // 点击"退出登录"按钮(真实 POST /auth/logout,删服务端会话)
  await screen.getByRole('button', { name: '退出登录' }).click();
  // 断言:右上角重新出现登录按钮(回到访客态)
  await expect(screen.getByRole('button', { name: /使用 GitHub 登录|登录暂不可用/ }).first()).toBeVisible();
});
