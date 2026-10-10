import { test, expect } from 'e2e';

/**
 * 补充页面行为 —— 答案卡,改动断言需人工批准。
 * 覆盖:档案阅读/版本对比(种子 docs/overview.md 两版)、旧版资料阅读弹窗、
 * 详情弹窗「原始正文」标签(种子 #11 带 Markdown 正文)、未知路由回退、
 * 候选搜索与恢复、每页 50 条时单页不渲染分页条。
 */

test('档案:阅读正文显示最新版本内容', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 打开 pilot/alpha 的项目档案页
  await app.open('/archives?repo=pilot/alpha');
  // 点击文件行的"阅读"按钮(默认阅读模式)
  await screen.getByRole('button', { name: '阅读' }).first().click();
  // 断言:正文区渲染出最新版本(r2)的内容
  await expect(browser.locator('.host-pre, .md').first()).toContainText('v2 概览');
});

test('档案:与上一版对比显示版本箭头与差异内容', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 打开 pilot/alpha 的项目档案页
  await app.open('/archives?repo=pilot/alpha');
  // 点击文件行的"对比"按钮
  await screen.getByRole('button', { name: '对比' }).first().click();
  // 断言:出现"旧版本 → 新版本"的修订号箭头(r1 → r2)
  await expect(browser.locator('.small')).toContainText('→');
  // 断言:差异区包含 r2 新增的文字
  await expect(browser.locator('.host-pre')).toContainText('认证模块');
});

test('旧版资料:阅读打开弹窗并渲染内容', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开旧版资料页
  await app.open('/legacy');
  // 点击该行的"阅读"按钮
  await screen.getByRole('button', { name: '阅读' }).click();
  // 拿到弹窗元素
  const dialog = screen.getByRole('dialog');
  // 断言:弹窗可见且标题是种子档案名
  await expect(dialog).toContainText('旧版项目档案:pilot/alpha');
  // 断言:内容区渲染出种子正文
  await expect(dialog).toContainText('旧版档案内容');
});

test('详情弹窗「原始正文」标签渲染 GitHub 原文', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击第一行(#11)的"详情"按钮
  await screen.getByRole('button', { name: '详情' }).first().click();
  // 切换到"原始正文"标签页
  await screen.getByRole('tab', { name: '原始正文' }).click();
  // 断言:正文区出现种子写入的 Markdown 内容
  await expect(screen.getByRole('dialog')).toContainText('复现步骤');
});

test('未知路由回退到 Issue 雷达', async ({ app, browser }) => {
  // 访问一个不存在的路径
  await app.open('/definitely/not/a/route');
  // 断言:被重定向到 /issues(不允许 404 白页)
  await expect(browser).toHaveURL(/\/issues/);
});

test('候选搜索命中单条并可清除恢复', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 在搜索框输入 star-03(只匹配一个候选)
  await screen.getByRole('textbox', { name: '搜索候选仓库' }).fill('star-03');
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:只剩 1 行
  await expect(rows).toHaveCount(1);
  // 断言:这一行就是 star-03
  await expect(rows.first()).toContainText('pilot/star-03');
  // 点击"清除搜索"恢复
  await screen.getByRole('button', { name: '清除搜索' }).click();
  // 断言:回到第一页 25 行
  await expect(rows).toHaveCount(25);
});

test('每页切换为 50 条时单页装下,分页条消失', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页(29 条)
  await app.open('/candidates');
  // 点击分页条里"每页"尺寸下拉框
  await browser.locator('.pager-size [role="combobox"]').click();
  // 在选项列表里选"50 条"
  await screen.getByRole('option', { name: '50 条' }).click();
  // 断言:全部 29 条都在这一页
  await expect(browser.locator('table.tbl tbody tr')).toHaveCount(29);
  // 断言:分页条不再渲染(单页规则,与雷达一致)
  await expect(browser.locator('.pager')).toHaveCount(0);
});
