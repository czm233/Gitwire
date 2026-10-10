import { test, expect } from 'e2e';

/**
 * Issue 雷达筛选与详情深度 —— 答案卡,改动断言需人工批准。
 * 种子契约:难度筛选「简单」命中 4 条(#11/#7/#9/#3);搜索 "login" 只命中 #11;
 * 「追踪中」分组只有 #3;「未入榜」分组只有 #9(评论过热),详情展示排除原因;
 * #11 详情含 问题摘要/解决方向/机会判断依据/状态证据(PR #20 仍开放)。
 * 对应 AGENTS.md:筛选提供恢复入口;模糊判定必须可解释(判定依据在详情)。
 */

test('难度筛选「简单」后可清除恢复', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击"Issue 难度"下拉框
  await screen.getByRole('combobox', { name: 'Issue 难度' }).click();
  // 在选项列表里选"简单"
  await screen.getByRole('option', { name: '简单' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:筛选后只剩 4 条(种子里难度为"简单"的 Issue 共 4 条)
  await expect(rows).toHaveCount(4);
  // 点击"清除筛选"按钮(筛选后必须出现恢复入口)
  await screen.getByRole('button', { name: '清除筛选' }).click();
  // 断言:清除后回到全部 5 条
  await expect(rows).toHaveCount(5);
});

test('文本搜索原文标题命中且可清除', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 在"筛选 Issue"输入框里输入英文关键词 login
  await screen.getByLabel('筛选 Issue').fill('login');
  // 找到表格所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:只命中 1 条
  await expect(rows).toHaveCount(1);
  // 断言:命中的就是标题含 login 的 #11
  await expect(rows.first()).toContainText('#11');
  // 点击"清除筛选"恢复
  await screen.getByRole('button', { name: '清除筛选' }).click();
  // 断言:回到全部 5 条
  await expect(rows).toHaveCount(5);
});

test('「追踪中」分组只列被追踪的 #3', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击"Issue 分组"下拉框
  await screen.getByRole('combobox', { name: 'Issue 分组' }).click();
  // 在选项列表里选"追踪中"
  await screen.getByRole('option', { name: '追踪中' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:只剩 1 行(种子:pilot 只追踪了 #3)
  await expect(rows).toHaveCount(1);
  // 断言:这一行就是 #3
  await expect(rows.first()).toContainText('#3');
});

test('「未入榜」分组只列 #9,详情给出排除原因', { session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击"Issue 分组"下拉框
  await screen.getByRole('combobox', { name: 'Issue 分组' }).click();
  // 在选项列表里选"未入榜"
  await screen.getByRole('option', { name: '未入榜' }).click();
  // 找到表格所有数据行
  const rows = browser.locator('.host-issues-table tbody tr');
  // 断言:只剩 1 行(只有 #9 被机会规则排除)
  await expect(rows).toHaveCount(1);
  // 断言:这一行就是 #9
  await expect(rows.first()).toContainText('#9');
  // 点击这一行的"详情"按钮打开弹窗
  await screen.getByRole('button', { name: '详情' }).first().click();
  // 切换到弹窗的"判断依据"标签页
  await screen.getByRole('tab', { name: '判断依据' }).click();
  // 拿到弹窗元素
  const dialog = screen.getByRole('dialog');
  // 断言:弹窗里有"机会判断依据"章节(模糊判定必须可解释)
  await expect(dialog).toContainText('机会判断依据');
  // 断言:排除原因写明"评论超过 15 条"(种子里 #9 有 99 条评论)
  await expect(dialog).toContainText('评论超过 15 条');
});

test('详情弹窗「分析」标签呈现摘要与解决方向', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击第一行(最新 #11)的"详情"按钮
  await screen.getByRole('button', { name: '详情' }).first().click();
  // 拿到弹窗元素(默认停在"分析"标签)
  const dialog = screen.getByRole('dialog');
  // 断言:有"问题摘要"章节
  await expect(dialog).toContainText('问题摘要');
  // 断言:摘要内容是种子写入的那句话
  await expect(dialog).toContainText('登录流程存在并发窗口');
  // 断言:有"问题与依据"章节
  await expect(dialog).toContainText('问题与依据');
  // 断言:依据内容包含"竞态"关键词
  await expect(dialog).toContainText('竞态');
  // 断言:有"解决方向"章节
  await expect(dialog).toContainText('解决方向');
  // 断言:解决方向提到"互斥锁"
  await expect(dialog).toContainText('互斥锁');
});

test('详情弹窗「判断依据」标签呈现判定理由与状态证据', { session: 'pilot' }, async ({ app, screen }) => {
  // 以 pilot 登录态打开雷达页
  await app.open('/issues');
  // 点击第一行(最新 #11)的"详情"按钮
  await screen.getByRole('button', { name: '详情' }).first().click();
  // 切换到"判断依据"标签页
  await screen.getByRole('tab', { name: '判断依据' }).click();
  // 拿到弹窗元素
  const dialog = screen.getByRole('dialog');
  // 断言:有"机会判断依据"章节
  await expect(dialog).toContainText('机会判断依据');
  // 断言:#11 是可上手状态,判定理由应是"符合当前机会筛选条件"
  await expect(dialog).toContainText('符合当前机会筛选条件');
  // 断言:有"状态证据"章节
  await expect(dialog).toContainText('状态证据');
  // 断言:证据里引用关联 PR pilot/alpha#20
  await expect(dialog).toContainText('pilot/alpha#20');
  // 断言:该 PR 的状态是"仍开放"
  await expect(dialog).toContainText('仍开放');
});
