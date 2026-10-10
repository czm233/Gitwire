import { test, expect } from 'e2e';

/**
 * Issue 雷达(登录态)核心规则 —— 答案卡,改动断言需人工批准。
 * 种子契约见 scripts/e2e_stack.py 的 SEED:
 *   5 条 Issue = 可上手 2(open/简单) + 已关闭 1 + 待分析 1 + 未入榜 1(#9 评论过热);
 *   最新 #11 带中文翻译与完整分析;#7 挂一条过期翻译(必须隐藏);pilot 追踪 #3。
 * 对应 AGENTS.md 规矩:默认分组「全部」、统计卡「全部」最左、单页不渲染分页条、
 * 最新在前、标题下只显示忠实翻译、翻译随原文失效。
 *
 * 登录方式:后端种子里预置了 BrowserSession,e2e 直接注入会话 cookie(GitWire 只有
 * GitHub OAuth,测试环境无法走真实授权)。断言全部为确定性 expect,零模型调用。
 */
const APP_URL = process.env.E2E_APP_URL ?? 'http://127.0.0.1:10249';

// 前置任务(每轮只跑一次):用种子里预置的会话 cookie 登录测试账户 pilot,存成名为 'pilot' 的会话
test.setup('前置:注入会话登录测试账户 pilot', { sessions: ['pilot'] }, async ({ app, screen, browser, session }) => {
  // 先打开任意页面,让浏览器处于本站域名下(设置 cookie 需要)
  await app.open('/');
  // 把种子里预置的登录会话 cookie 直接写入浏览器(绕过 GitHub OAuth)
  await browser.setCookies([{ url: APP_URL, name: 'gitwire_session', value: 'e2e-pilot-session' }]);
  // 再打开雷达页
  await app.open('/issues');
  // 断言:右上角出现"账户菜单"按钮,说明确实处于登录态
  await expect(screen.getByRole('button', { name: /账户菜单/ })).toBeVisible();
  // 把当前登录态保存成名为 'pilot' 的会话,后续测试声明 { session: 'pilot' } 即可复用
  await session.save('pilot');
});

test('统计条:六张卡,「全部」最左且默认激活', { tags: ['smoke'],  session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 找到所有统计卡元素
  const cards = browser.locator('.host-stat');
  // 断言:统计卡数量恰好 6 张
  await expect(cards).toHaveCount(6);
  // 断言:第一张卡的内容包含"全部"(全部卡必须排在最左)
  await expect(cards.first()).toContainText('全部');
  // 断言:带 active 高亮样式的统计卡内容包含"全部"(默认激活的必须是它)
  await expect(browser.locator('.host-stat.active').first()).toContainText('全部');
});

test('统计数字与种子数据一致(全部5·可上手2·已关闭1·未入榜1·待分析1)', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 找到所有统计卡元素
  const cards = browser.locator('.host-stat');
  // 断言:统计卡数量恰好 6 张
  await expect(cards).toHaveCount(6);
  // 期望的六张卡:标签 + 数字(全部=5 · 可上手=2 · 有人做了=0 · 已关闭=1 · 未入榜=1 · 待分析=1)
  const expected = [['全部', '5'], ['可上手', '2'], ['有人做了', '0'], ['已关闭', '1'], ['未入榜', '1'], ['待分析', '1']];
  // 逐张核对:第 i 张卡必须同时包含期望的标签和数字
  for (let i = 0; i < expected.length; i++) {
    // 断言:第 i 张卡包含标签(隐含了卡片顺序正确)
    await expect(cards.nth(i)).toContainText(expected[i][0]);
    // 断言:第 i 张卡包含统计数字
    await expect(cards.nth(i)).toContainText(expected[i][1]);
  }
});

test('默认分组「全部」:五条全列出,最新在前,过期翻译不显示', { tags: ['smoke'],  session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开雷达页(默认分组就是"全部")
  await app.open('/issues');
  // 找到雷达表格的所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:5 条种子 Issue 全部列出
  await expect(rows).toHaveCount(5);
  // 断言:第一行包含 #11(1 小时前创建的最新 Issue 必须排最前)
  await expect(rows.first()).toContainText('#11');
  // 断言:第一行的标题原文正确
  await expect(rows.first()).toContainText('Fix login race condition');
  // 找到所有"标题下方翻译行"元素
  const translation = browser.locator('.host-issue-translation');
  // 断言:翻译行只有 1 条(#7 挂着过期翻译,原文变了必须隐藏,唯一显示的属于 #11)
  await expect(translation).toHaveCount(1);
  // 断言:这行翻译的文字就是 #11 的中文标题
  await expect(translation).toHaveText('修复登录竞态条件');
});

test('单页数据不渲染分页条', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 断言:数据行有 5 条(少于每页 25 条,属于单页场景)
  await expect(browser.locator('.host-issues-table tbody tr')).toHaveCount(5);
  // 断言:分页条元素数量为 0(只有一页时整个分页条不渲染)
  await expect(browser.locator('.pager')).toHaveCount(0);
});

test('切换分组到「已关闭」只剩那一条', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击"Issue 分组"下拉框(按无障碍角色+名称定位)
  await screen.getByRole('combobox', { name: 'Issue 分组' }).click();
  // 在弹出的选项列表里点击"已关闭"
  await screen.getByRole('option', { name: '已关闭' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:只剩 1 行(种子里已关闭的 Issue 只有 #3)
  await expect(rows).toHaveCount(1);
  // 断言:这一行就是 #3
  await expect(rows.first()).toContainText('#3');
});

test('详情以 Modal 弹窗打开', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击第一行的"详情"按钮
  await screen.getByRole('button', { name: '详情' }).first().click();
  // 断言:页面上出现对话框(详情必须以 Modal 呈现,不能跳页)
  await expect(screen.getByRole('dialog')).toBeVisible();
});

test('监控清单:两个种子仓库,状态监控中并显示追踪编号', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开监控清单页
  await app.open('/repos');
  // 断言:右上角存在"＋ 添加监控"主按钮(登录态才显示)
  await expect(screen.getByRole('button', { name: '＋ 添加监控' })).toBeVisible();
  // 断言:表格恰好 2 行(pilot 订阅了 alpha 和 beta 两个仓库)
  await expect(browser.locator('table.tbl tbody tr')).toHaveCount(2);
  // 断言:第一行包含 pilot/ 前缀的仓库名
  await expect(browser.locator('table.tbl tbody tr').first()).toContainText('pilot/');
  // 断言:表格里出现"监控中"状态字样
  await expect(browser.locator('table.tbl tbody')).toContainText('监控中');
  // 断言:alpha 行显示追踪中的 Issue 编号(种子:追踪 #3)
  await expect(browser.locator('table.tbl tbody')).toContainText('追踪 #3');
});
