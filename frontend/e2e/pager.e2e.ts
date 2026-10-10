import { test, expect } from 'e2e';

/**
 * 分页条(ListPager)行为 —— 答案卡,改动断言需人工批准。
 * 以候选仓库页(种子 29 条)为载体。对应 AGENTS.md:总数 · 每页 10/25/50 ·
 * 页码数字当前页高亮 · 上一页/下一页按位置出现 · 不用首页/末页按钮。
 */

test('分页条显示总数,第一页只有下一页', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页(29 条 Star,每页 25 → 2 页)
  await app.open('/candidates');
  // 断言:分页条上显示"29 条"总数
  await expect(browser.locator('.pager')).toContainText('29 条');
  // 断言:"下一页"按钮可见(第一页必须有)
  await expect(screen.getByRole('button', { name: '下一页' })).toBeVisible();
  // 断言:"上一页"按钮数量为 0(第一页不该有)
  await expect(screen.getByRole('button', { name: '上一页' })).toHaveCount(0);
});

test('每页切换为 10 条后页码变为 1 2 3,翻页内容正确', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开候选仓库页
  await app.open('/candidates');
  // 点击分页条里"每页"尺寸下拉框
  await browser.locator('.pager-size [role="combobox"]').click();
  // 在选项列表里选"10 条"
  await screen.getByRole('option', { name: '10 条' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:本页 10 行(29 条按每页 10 切分)
  await expect(rows).toHaveCount(10);
  // 断言:页码按钮 1、2、3 都出现(29/10 = 3 页)
  for (const n of ['1', '2', '3']) {
    await expect(browser.locator('.pager .pager-num').filter({ hasText: n })).toBeVisible();
  }
  // 点击"下一页"翻到第 2 页
  await screen.getByRole('button', { name: '下一页' }).click();
  // 断言:第 2 页也是 10 行
  await expect(rows).toHaveCount(10);
  // 断言:第 2 页第一行是 star-11(第 11 新的)
  await expect(rows.first()).toContainText('pilot/star-11');
  // 再点"下一页"翻到第 3 页(末页)
  await screen.getByRole('button', { name: '下一页' }).click();
  // 断言:末页 9 行(29 - 20)
  await expect(rows).toHaveCount(9);
  // 断言:末页最后一行是 star-00(无 Star 时间的排最后)
  await expect(rows.last()).toContainText('pilot/star-00');
  // 断言:末页没有"下一页"按钮
  await expect(screen.getByRole('button', { name: '下一页' })).toHaveCount(0);
  // 断言:末页出现"上一页"按钮
  await expect(screen.getByRole('button', { name: '上一页' })).toBeVisible();
});
