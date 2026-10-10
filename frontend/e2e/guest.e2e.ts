import { test, expect } from 'e2e';

/**
 * 访客(未登录)公共面规则。
 * 对应 AGENTS.md:`/` 重定向到 /issues;Issue 雷达是 01 号导航项;
 * 未登录访问个人页面说明登录后用途并提供公开浏览入口;右上角仅显示登录图标。
 */

test('访客:根路径重定向到公开 Issue 雷达', { tags: ['smoke'] }, async ({ app, browser }) => {
  // 打开网站根路径 /
  await app.open('/');
  // 断言:浏览器地址最终停在 /issues(根路径必须重定向到雷达)
  await expect(browser).toHaveURL(/\/issues/);
});

test('访客:雷达显示公开浏览引导,不渲染个人雷达', { tags: ['smoke'] }, async ({ app, screen, browser }) => {
  // 打开 Issue 雷达页
  await app.open('/issues');
  // 断言:页面上出现"查看公开项目的 Issue"这个大标题(访客引导卡)
  await expect(screen.getByRole('heading', { name: '查看公开项目的 Issue' })).toBeVisible();
  // 断言:访客不该看到个人统计条,元素数量必须是 0
  await expect(browser.locator('.host-stats')).toHaveCount(0);
  // 断言:访客不该看到 issue 表格,元素数量必须是 0
  await expect(browser.locator('.host-issues-table')).toHaveCount(0);
});

test('访客:右上角是登录图标按钮', async ({ app, screen }) => {
  // 打开 Issue 雷达页
  await app.open('/issues');
  // 断言:右上角存在登录按钮(三种文案任一,取决于登录入口状态)
  await expect(screen.getByRole('button', { name: /使用 GitHub 登录|正在前往 GitHub|登录暂不可用/ })).toBeVisible();
});

test('访客:个人页说明登录用途并提供公开浏览入口', async ({ app, screen }) => {
  // 打开监控清单页(个人页面)
  await app.open('/repos');
  // 断言:出现"登录后管理监控清单"的说明标题
  await expect(screen.getByRole('heading', { name: '登录后管理监控清单' })).toBeVisible();
  // 断言:提供"先浏览公开项目 →"的公开浏览链接
  await expect(screen.getByRole('link', { name: '先浏览公开项目 →' })).toBeVisible();
});
