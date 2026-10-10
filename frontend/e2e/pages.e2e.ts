import { test, expect } from 'e2e';

/**
 * 运行历史 / 警报 / 每日晨报 / 项目档案 / 设置 —— 答案卡,改动断言需人工批准。
 * 种子契约:3 条任务(scan·已完成·count 5 / analyze_repo·失败·连接超时·3 次 / digest·等待中·2 次)、
 * 2 条警报(一读一未读)、1 篇晨报、docs/overview.md 两个版本。
 */

test('运行历史:任务类型与结果分列,状态与尝试次数如实呈现', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开运行历史页
  await app.open('/runs');
  // 找到表格所有数据行(注意:雷达访问会自动排入翻译任务,总行数是动态的)
  const rows = browser.locator('table.tbl tbody tr');
  // 在所有行里筛出结果为"本次处理 5 项"的行(种子那条成功的 scan;
  // 访客查询项目会自动排入新的 scan 任务,所以不能按类型名数行数)
  const scan = rows.filter({ hasText: '本次处理 5 项' });
  // 断言:恰好 1 行
  await expect(scan).toHaveCount(1);
  // 断言:该行类型是"更新仓库动态"
  await expect(scan.first()).toContainText('更新仓库动态');
  // 断言:该行状态是"成功"
  await expect(scan.first()).toContainText('成功');
  // 筛出类型为"更新项目档案"(analyze_repo)的行
  const failed = rows.filter({ hasText: '更新项目档案' });
  // 断言:该行状态是"失败"
  await expect(failed.first()).toContainText('失败');
  // 断言:失败原因显示"连接超时"(种子的错误信息)
  await expect(failed.first()).toContainText('连接超时');
  // 断言:显示尝试了"3 次"
  await expect(failed.first()).toContainText('3 次');
  // 筛出类型为"生成晨报"(digest)的行
  const digest = rows.filter({ hasText: '生成晨报' });
  // 断言:该行状态是"等待中"
  await expect(digest.first()).toContainText('等待中');
});

test('警报:已读与未读状态区分,未读提供标为已读', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开警报页
  await app.open('/alerts');
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:共 2 条警报(种子:一读一未读)
  await expect(rows).toHaveCount(2);
  // 筛出"发现可上手 Issue"那行(种子里已读的那条)
  const read = rows.filter({ hasText: '发现可上手 Issue' });
  // 断言:状态显示"已读"
  await expect(read.first()).toContainText('已读');
  // 筛出"发布 v2.0"那行(未读的那条)
  const unread = rows.filter({ hasText: '发布 v2.0' });
  // 断言:该行提供"标为已读"按钮
  await expect(unread.first()).toContainText('标为已读');
  // 断言:第一行是"发布 v2.0"(最新事件必须排最前)
  await expect(rows.first()).toContainText('发布 v2.0');
});

test('每日晨报渲染种子内容', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开每日晨报页
  await app.open('/daily');
  // 断言:晨报文章区块恰好 1 篇(种子 1 篇)
  await expect(browser.locator('.host-digest')).toHaveCount(1);
  // 断言:内容包含标题"每日晨报"
  await expect(browser.locator('.host-digest')).toContainText('每日晨报');
  // 断言:内容提到 pilot/alpha 的动态
  await expect(browser.locator('.host-digest')).toContainText('pilot/alpha');
});

test('项目档案:文件列表最新版本在前,提供阅读入口', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 打开 pilot/alpha 的项目档案页(带仓库参数)
  await app.open('/archives?repo=pilot/alpha');
  // 断言:文件列表出现种子写入的 docs/overview.md
  await expect(browser.locator('table.tbl tbody')).toContainText('docs/overview.md');
  // 断言:存在"阅读"按钮(点开可看文档内容)
  await expect(screen.getByRole('button', { name: '阅读' }).first()).toBeVisible();
});

test('设置页:登录账户显示只读连接徽章', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开设置页
  await app.open('/settings');
  // 断言:显示登录名 pilot
  await expect(screen.getByText('pilot')).toBeVisible();
  // 断言:显示"已连接 · 只读"徽章(明确告知授权范围)
  await expect(screen.getByText('已连接 · 只读')).toBeVisible();
});
