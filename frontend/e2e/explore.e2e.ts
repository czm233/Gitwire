import { test, expect } from 'e2e';

/**
 * 访客「添加项目」查询流(/explore)—— 答案卡,改动断言需人工批准。
 * 种子契约:alpha 仓库 checked_at 新鲜,explore 命中 DB 缓存(不依赖 GitHub);
 * alpha 名下 3 条 Issue(#11/#7/#9)。对应 AGENTS.md:访客可查询公开项目、
 * 无需登录;加入监控入口仅登录后可见。
 */

test('访客:添加项目查询已知公开仓库,列表切换到该项目', async ({ app, screen, browser }) => {
  // 以访客身份打开雷达页
  await app.open('/issues');
  // 点击右上角"＋ 添加项目"按钮,打开查询弹窗
  await screen.getByRole('button', { name: '＋ 添加项目' }).click();
  // 在"公开仓库"输入框填入种子仓库地址
  await screen.getByRole('textbox', { name: '公开仓库' }).fill('pilot/alpha');
  // 点击"查询项目"提交
  await screen.getByRole('button', { name: '查询项目' }).click();
  // 断言:弹窗关闭(查询成功后自动关闭)
  await expect(screen.getByRole('dialog')).toHaveCount(0);
  // 断言:筛选区出现 pilot/alpha 的仓库徽章(当前查看范围)
  await expect(browser.locator('.badge')).toContainText('pilot/alpha');
  // 找到雷达表格所有行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:只显示 alpha 的 3 条 Issue(#11/#7/#9)
  await expect(rows).toHaveCount(3);
  // 断言:第一行仍是 #11(项目内最新在前)
  await expect(rows.first()).toContainText('#11');
});

test('访客:查询项目不显示"加入监控"入口(登录后才可保存)', async ({ app, screen }) => {
  // 以访客身份打开雷达页
  await app.open('/issues');
  // 打开添加项目弹窗并查询 alpha
  await screen.getByRole('button', { name: '＋ 添加项目' }).click();
  await screen.getByRole('textbox', { name: '公开仓库' }).fill('pilot/alpha');
  await screen.getByRole('button', { name: '查询项目' }).click();
  // 断言:页面上没有"加入监控"链接(该入口仅登录用户可见)
  await expect(screen.getByRole('link', { name: '加入监控' })).toHaveCount(0);
});

test('访客:查询不存在的仓库显示错误而非静默', async ({ app, screen }) => {
  // 以访客身份打开雷达页
  await app.open('/issues');
  // 打开添加项目弹窗
  await screen.getByRole('button', { name: '＋ 添加项目' }).click();
  // 填一个 GitHub 上不存在的仓库地址
  await screen.getByRole('textbox', { name: '公开仓库' }).fill('gitwire-e2e/definitely-not-exist');
  // 点击"查询项目"提交(后端会真实尝试解析该仓库,失败返回错误)
  await screen.getByRole('button', { name: '查询项目' }).click();
  // 断言:弹窗内出现错误提示框(查询失败必须可见)
  await expect(screen.getByRole('alert')).toBeVisible();
});
