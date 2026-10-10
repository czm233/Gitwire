import { test, expect } from 'e2e';

/**
 * 请求拦截(mock)测试 —— 前端契约层,答案卡,改动断言需人工批准。
 * 用 browser.route 拦截指定 API 请求并返回伪造响应,验证前端对
 * "成功响应"的处理是否正确(真实后端的成功路径需要 GitHub 授权,由 pytest 层覆盖)。
 * 拦截只作用于当前测试的浏览器上下文,不污染其他测试。
 */

test('添加监控:成功响应后关闭弹窗且无错误(mock 契约)', { session: 'writer' }, async ({ app, screen, browser }) => {
  // 拦截 /api/repos:POST 伪造成功响应;GET 放行走真实后端
  await browser.route('**/api/repos', async (route) => {
    // 按请求方法分流:提交监控的 POST 返回伪造成功 JSON
    if (route.request.method === 'POST') {
      await route.fulfill({ status: 200, json: { ok: true } });
    } else {
      // 列表查询放行到真实后端
      await route.fallback();
    }
  });
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 打开添加监控弹窗
  await screen.getByRole('button', { name: '＋ 添加监控' }).click();
  // 在"公开仓库"输入框填一个仓库地址
  await screen.getByRole('textbox', { name: '公开仓库' }).fill('pilot/mock-repo');
  // 点击"加入监控"提交(命中 mock 的成功响应)
  await screen.getByRole('button', { name: '加入监控' }).click();
  // 断言:弹窗关闭(成功路径的既定行为)
  await expect(screen.getByRole('dialog')).toHaveCount(0);
  // 断言:页面上没有错误提示框(成功不该伴随报错)
  await expect(screen.getByRole('alert')).toHaveCount(0);
});

test('候选同步:任务进行中按钮锁定,完成后恢复(mock 契约)', { session: 'writer' }, async ({ app, screen, browser }) => {
  // 计数器:第一次查任务返回 running,之后返回 completed
  let polled = 0;
  // 拦截同步提交:POST 返回伪造的任务 id
  await browser.route('**/api/candidates/sync', async (route) => {
    await route.fulfill({ status: 200, json: { job_id: 'mock-job' } });
  });
  // 拦截任务查询:第一次进行中,第二次完成
  await browser.route('**/api/jobs/mock-job', async (route) => {
    polled += 1;
    // 前端只读取 status 字段,返回最小结构即可
    const status = polled === 1 ? 'running' : 'completed';
    await route.fulfill({ status: 200, json: { id: 'mock-job', status } });
  });
  // 以 writer 登录态打开候选仓库页
  await app.open('/candidates');
  // 点击"同步 GitHub 列表"按钮发起同步
  await screen.getByRole('button', { name: /同步 GitHub 列表|正在同步…/ }).click();
  // 断言:按钮进入"正在同步…"锁定态(防止重复提交)
  await expect(screen.getByRole('button', { name: '正在同步…' })).toBeVisible();
  // 断言:任务完成后按钮恢复可用文案
  await expect(screen.getByRole('button', { name: '同步 GitHub 列表' })).toBeVisible();
});

test('监控配置:弹窗呈现全部配置字段并可保存关闭(真实写路径)', { session: 'writer' }, async ({ app, screen }) => {
  // 以 writer 登录态打开监控清单页
  await app.open('/repos');
  // 找到 pilot/gamma 那一行,点击"配置"按钮
  await screen.getByRole('button', { name: '配置' }).click();
  // 拿到配置弹窗元素
  const dialog = screen.getByRole('dialog');
  // 断言:弹窗标题标明 gamma 仓库
  await expect(dialog).toContainText('监控配置 · pilot/gamma');
  // 断言:包含"追踪 Issue 编号"输入区
  await expect(dialog).toContainText('追踪 Issue 编号');
  // 断言:包含"机会筛选条件"数值区
  await expect(dialog).toContainText('机会筛选条件');
  // 断言:包含"提醒关键词"输入区
  await expect(dialog).toContainText('提醒关键词');
  // 点击"保存配置"(真实 PATCH 落库)
  await screen.getByRole('button', { name: '保存配置' }).click();
  // 断言:保存成功后弹窗关闭
  await expect(screen.getByRole('dialog')).toHaveCount(0);
});
