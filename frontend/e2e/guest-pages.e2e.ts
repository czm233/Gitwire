import { test, expect } from 'e2e';

/**
 * 访客引导全覆盖 —— 答案卡,改动断言需人工批准。
 * 对应 AGENTS.md:未登录访问个人页面时,说明该页登录后的用途,
 * 并提供公开浏览入口(「先浏览公开项目 →」)。core.tsx 的 prompts
 * 为每个个人页定义了专属说明标题。
 */

test('访客:各个人页说明登录用途并提供公开浏览入口', async ({ app, screen }) => {
  // 逐个访问四个个人页,核对各自的登录说明标题与公开浏览入口
  const pages = [
    ['/candidates', '登录后同步 GitHub 仓库'],
    ['/runs', '登录后查看运行历史'],
    ['/daily', '登录后查看每日晨报'],
    ['/alerts', '登录后查看个人警报'],
  ];
  for (const [path, heading] of pages) {
    // 访问该个人页(访客态)
    await app.open(path);
    // 断言:出现该页专属的登录用途说明标题
    await expect(screen.getByRole('heading', { name: heading })).toBeVisible();
    // 断言:提供"先浏览公开项目 →"的公开浏览入口
    await expect(screen.getByRole('link', { name: '先浏览公开项目 →' })).toBeVisible();
  }
});
