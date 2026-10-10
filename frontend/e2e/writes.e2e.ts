import { test, expect } from 'e2e';

/**
 * 写操作交互路径 —— 答案卡,改动断言需人工批准。
 * 全部作用于第二种子用户 writer(自己的仓库 pilot/gamma、自己的警报),
 * 不影响 pilot 名下的只读断言;并行运行互不踩踏。
 * 前端写操作自动携带 /auth/me 下发的 CSRF 令牌,因此真实走 API 落库。
 * 对应 AGENTS.md:确认弹窗统一用 Confirm(禁止原生 confirm);添加监控默认配方
 * 「档案同步 + issue 雷达」;失败要显示错误而非静默。
 */
const APP_URL = process.env.E2E_APP_URL ?? 'http://127.0.0.1:10249';

// 前置任务:注入 writer 的会话 cookie 并保存为 'writer' 会话(写操作专用账号)
test.setup('前置:注入会话登录测试账户 writer', { sessions: ['writer'] }, async ({ app, screen, browser, session }) => {
  // 先打开任意页面,让浏览器处于本站域名下
  await app.open('/');
  // 写入种子里预置的 writer 登录会话 cookie
  await browser.setCookies([{ url: APP_URL, name: 'gitwire_session', value: 'e2e-writer-session' }]);
  // 打开监控清单页
  await app.open('/repos');
  // 断言:出现"＋ 添加监控"按钮,证明已处于 writer 登录态
  await expect(screen.getByRole('button', { name: '＋ 添加监控' })).toBeVisible();
  // 保存会话,后续测试用 { session: 'writer' } 复用
  await session.save('writer');
});

test('暂停监控后状态翻转,恢复后回到监控中', { tags: ['smoke'],  session: 'writer' }, async ({ app, screen, browser }) => {
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 找到 pilot/gamma 那一行(writer 专属仓库)
  const gammaRow = browser.locator('table.tbl tbody tr').filter({ hasText: 'pilot/gamma' });
  // 断言:初始状态是"监控中"
  await expect(gammaRow).toContainText('监控中');
  // 点击该行的"暂停"按钮(真实写操作,经 API 落库)
  await gammaRow.getByRole('button', { name: '暂停' }).click();
  // 断言:状态变成"已暂停"
  await expect(gammaRow).toContainText('已暂停');
  // 再点击"恢复"按钮
  await gammaRow.getByRole('button', { name: '恢复' }).click();
  // 断言:状态回到"监控中"(把数据还原,不影响其他测试)
  await expect(gammaRow).toContainText('监控中');
});

test('移除监控走确认弹窗,取消则不生效', { session: 'writer' }, async ({ app, screen, browser }) => {
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 找到 pilot/gamma 那一行
  const gammaRow = browser.locator('table.tbl tbody tr').filter({ hasText: 'pilot/gamma' });
  // 点击该行的"移除"按钮
  await gammaRow.getByRole('button', { name: '移除' }).click();
  // 找到确认弹窗(AlertDialog,自定义 Confirm 组件;禁止浏览器原生 confirm)
  const dialog = screen.getByRole('alertdialog');
  // 断言:确认弹窗可见
  await expect(dialog).toBeVisible();
  // 点击弹窗里的"取消"
  await screen.getByRole('button', { name: '取消' }).click();
  // 断言:gamma 行还在(取消后移除不生效)
  await expect(gammaRow).toHaveCount(1);
  // 断言:该行仍是"监控中"
  await expect(gammaRow).toContainText('监控中');
});

test('添加监控弹窗默认勾选「档案同步 + Issue 雷达」', { session: 'writer' }, async ({ app, screen }) => {
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 点击右上角"＋ 添加监控"主按钮
  await screen.getByRole('button', { name: '＋ 添加监控' }).click();
  // 拿到弹窗元素
  const dialog = screen.getByRole('dialog');
  // 断言:弹窗标题是"添加监控"
  await expect(dialog).toContainText('添加监控');
  // 断言:"档案同步"复选框默认勾选(起步配方约定)
  await expect(screen.getByRole('checkbox', { name: /档案同步/ })).toBeChecked();
  // 断言:"Issue 雷达"复选框默认勾选
  await expect(screen.getByRole('checkbox', { name: /Issue 雷达/ })).toBeChecked();
});

test('提交无效仓库显示错误提示而非静默失败', { session: 'writer' }, async ({ app, screen }) => {
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 打开添加监控弹窗
  await screen.getByRole('button', { name: '＋ 添加监控' }).click();
  // 在"公开仓库"输入框填一个不存在的仓库地址
  await screen.getByRole('textbox', { name: '公开仓库' }).fill('definitely/not-exist');
  // 点击"加入监控"提交
  await screen.getByRole('button', { name: '加入监控' }).click();
  // 断言:页面出现错误提示框(role=alert),失败必须可见,不能静默
  await expect(screen.getByRole('alert')).toBeVisible();
});

test('警报可标记为已读', { session: 'writer' }, async ({ app, screen, browser }) => {
  // 以 writer 登录态打开警报页
  await app.open('/alerts');
  // 找到 writer 专属警报那一行
  const row = browser.locator('table.tbl tbody tr').filter({ hasText: 'writer 的警报' });
  // 断言:恰好 1 行
  await expect(row).toHaveCount(1);
  // 点击该行的"标为已读"按钮(真实写操作)
  await row.getByRole('button', { name: '标为已读' }).click();
  // 断言:状态变成"已读"
  await expect(row).toContainText('已读');
});
