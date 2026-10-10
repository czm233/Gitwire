import { test, expect } from 'e2e';

/**
 * 次要页面 —— 答案卡,改动断言需人工批准。
 * 种子契约:投递记录 2 条(已发送的警报邮件 + 失败的晨报,失败带原因与重试);
 * 旧版资料 1 条(旧版文档);退订页无 token 给出引导、伪 token 报错不静默;
 * 设置页访客看到登录说明。对应 AGENTS.md:请求失败提供重试、不显示成"暂无数据"。
 */

test('投递记录:状态与失败原因分列,失败可重试', { tags: ['smoke'],  session: 'pilot' }, async ({ app, screen, browser }) => {
  // 以 pilot 登录态打开邮件投递记录页
  await app.open('/deliveries');
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:共 2 条投递记录(种子:一成功一失败)
  await expect(rows).toHaveCount(2);
  // 断言:第一行是警报邮件(1 小时前创建,最新在前)
  await expect(rows.first()).toContainText('Gitwire 警报:发现可上手 Issue');
  // 断言:该行状态是"已交给邮件服务器"(发送成功)
  await expect(rows.first()).toContainText('已交给邮件服务器');
  // 筛出晨报邮件那一行(失败的那条)
  const failed = rows.filter({ hasText: 'Gitwire 每日晨报' });
  // 断言:状态是"发送失败"
  await expect(failed.first()).toContainText('发送失败');
  // 断言:失败原因显示 SMTP 550(种子的错误信息)
  await expect(failed.first()).toContainText('SMTP 550');
  // 断言:失败行提供"重试"按钮
  await expect(failed.first().getByRole('button', { name: '重试' })).toBeVisible();
});

test('旧版资料列表与类型正确显示', { session: 'pilot' }, async ({ app, browser }) => {
  // 以 pilot 登录态打开旧版资料页
  await app.open('/legacy');
  // 找到表格所有数据行
  const rows = browser.locator('table.tbl tbody tr');
  // 断言:恰好 1 条(种子 1 条旧版资料)
  await expect(rows).toHaveCount(1);
  // 断言:标题是种子写入的旧版档案
  await expect(rows.first()).toContainText('旧版项目档案:pilot/alpha');
  // 断言:类型列显示"旧版文档"
  await expect(rows.first()).toContainText('旧版文档');
});

test('退订页缺 token 时给出引导而非空白', async ({ app, screen }) => {
  // 不带任何参数打开退订页(访客,无需登录)
  await app.open('/unsubscribe');
  // 断言:显示"缺少退订链接"的引导文案,不能是空白页
  await expect(screen.getByText(/缺少退订链接/)).toBeVisible();
});

test('退订页无效 token 点击确认后显示错误', async ({ app, screen }) => {
  // 带一个伪造的 token 打开退订页
  await app.open('/unsubscribe?token=definitely-invalid');
  // 点击"确认停止邮件提醒"按钮
  await screen.getByRole('button', { name: '确认停止邮件提醒' }).click();
  // 断言:出现错误提示框(无效 token 必须报错,不能静默"成功")
  await expect(screen.getByRole('alert')).toBeVisible();
});

test('设置页访客看到登录说明与入口', async ({ app, screen }) => {
  // 以访客身份打开设置页(不注入会话)
  await app.open('/settings');
  // 断言:显示"GitHub 账户"卡片标题
  await expect(screen.getByText('GitHub 账户')).toBeVisible();
  // 断言:显示访客说明文案
  await expect(screen.getByText(/访客可以查询公开项目/)).toBeVisible();
  // 断言:页面存在登录按钮(取第一个:顶栏图标与卡片内链接同名)
  await expect(screen.getByRole('button', { name: /使用 GitHub 登录|登录暂不可用/ }).first()).toBeVisible();
});
