import { test, expect } from 'e2e';

/**
 * 候选仓库页规则 —— 答案卡,改动断言需人工批准。
 * 种子契约:29 条我的 Star(star-01 最新;star-00 无 Star 时间排最后;pilot/alpha 已监控)、
 * 2 条已忽略、3 条我的公开仓库。对应 AGENTS.md:Star 时间用 GitHub 的 starred_at 倒序,
 * 不得用导入时间替代;未获取时间的排最后;我的公开仓库不显示 Star 时间列;
 * 分页用 ListPager(上一页/下一页按位置出现)。
 */

test('我的 Star 默认视图:最新 Star 在最前,已监控行显示已监控', { tags: ['smoke'],  session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开候选仓库页(默认来源=我的 Star)
  await app.open('/candidates');
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:本页 25 行(共 29 条,每页 25)
  await expect(rows).toHaveCount(25);
  // 断言:第一行是 star-01(1 小时前 Star 的,必须排最前)
  await expect(rows.first()).toContainText('pilot/star-01');
  // 找到包含 pilot/alpha 的那一行(它已被监控)
  const monitored = rows.filter({ hasText: 'pilot/alpha' });
  // 断言:这样的行恰好 1 行
  await expect(monitored).toHaveCount(1);
  // 断言:该行状态显示"已监控"
  await expect(monitored.first()).toContainText('已监控');
});

test('Star 时间列存在且标注最新在前', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 断言:存在 Star 时间列头(悬停说明写明"你在 GitHub 点 Star 的时间,最新在前")
  await expect(browser.locator('th[title*="Star 的时间"]')).toBeVisible();
});

test('分页:第一页只有下一页,翻到末页只有上一页,无时间的排在最后', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 断言:"下一页"按钮可见(第一页必须有下一页)
  await expect(screen.getByRole('button', { name: '下一页' })).toBeVisible();
  // 断言:"上一页"按钮数量为 0(第一页不该有上一页)
  await expect(screen.getByRole('button', { name: '上一页' })).toHaveCount(0);
  // 点击"下一页"翻到第 2 页
  await screen.getByRole('button', { name: '下一页' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:第 2 页有 4 行(29 - 25)
  await expect(rows).toHaveCount(4);
  // 断言:第 2 页第一行是 star-26(第 26 新的)
  await expect(rows.first()).toContainText('pilot/star-26');
  // 断言:最后一行是 star-00(没拿到 Star 时间的必须排最后)
  await expect(rows.last()).toContainText('pilot/star-00');
  // 断言:该行显示"未获取"字样(如实标注,不用导入时间冒充)
  await expect(rows.last()).toContainText('未获取');
  // 断言:末页出现"上一页"按钮
  await expect(screen.getByRole('button', { name: '上一页' })).toBeVisible();
  // 断言:末页没有"下一页"按钮
  await expect(screen.getByRole('button', { name: '下一页' })).toHaveCount(0);
});

test('切换到我的公开仓库:不显示 Star 时间列,只列公开仓库', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 点击"仓库来源"下拉框
  await screen.getByRole('combobox', { name: '仓库来源' }).click();
  // 在选项列表里选"我的公开仓库"
  await screen.getByRole('option', { name: '我的公开仓库' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:3 行(种子 3 个公开仓库)
  await expect(rows).toHaveCount(3);
  // 断言:Star 时间列头数量为 0(公开仓库不显示 Star 时间列)
  await expect(browser.locator('th[title*="Star 的时间"]')).toHaveCount(0);
  // 断言:第一行是 owned 开头的公开仓库
  await expect(rows.first()).toContainText('pilot/owned-');
});

test('已忽略范围只显示被忽略的候选', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 点击"候选范围"下拉框
  await screen.getByRole('combobox', { name: '候选范围' }).click();
  // 在选项列表里选"已忽略"
  await screen.getByRole('option', { name: '已忽略' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:只剩 2 行(种子 2 个已忽略候选)
  await expect(rows).toHaveCount(2);
  // 断言:第一行是 old-idea 开头的被忽略仓库
  await expect(rows.first()).toContainText('pilot/old-idea-');
});
