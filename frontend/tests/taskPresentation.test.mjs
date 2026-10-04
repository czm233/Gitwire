import assert from 'node:assert/strict';
import test from 'node:test';
import { taskStatus, taskSummary } from '../src/hosted/taskPresentation.ts';

const job = (overrides = {}) => ({ kind: 'watch_issues', status: 'completed', result: {}, ...overrides });

test('a successful watch without a public count still has an explicit outcome', () => {
  assert.equal(taskStatus(job()), '成功');
  assert.equal(taskSummary(job()), '已完成 Issue 动态检查');
  assert.doesNotMatch(taskSummary(job()), /0 项|未发现变化/);
});

test('every other successful task has a fallback outcome without result metadata', () => {
  for (const kind of ['analyze_claim', 'analyze_issue', 'analyze_repo', 'digest']) {
    assert.equal(taskStatus(job({ kind })), '成功');
    assert.equal(taskSummary(job({ kind })), '处理成功');
  }
});

test('failure overrides result metadata and retains the reason', () => {
  const failed = job({ status: 'failed', error: '连接超时', result: { count: 2 } });
  assert.equal(taskStatus(failed), '失败');
  assert.equal(taskSummary(failed), '连接超时');
  assert.match(taskSummary(job({ status: 'failed' })), /处理失败/);
});

test('previous errors do not make a waiting or running task look failed', () => {
  assert.equal(taskStatus(job({ status: 'pending', error: '额度不足' })), '等待中');
  assert.equal(taskSummary(job({ status: 'pending', error: '额度不足' })), '等待继续处理：额度不足');
  assert.equal(taskSummary(job({ status: 'pending' })), '已排队，等待处理');
  const running = job({ status: 'running', error: '上次连接超时' });
  assert.equal(taskStatus(running), '运行中');
  assert.equal(taskSummary(running), '正在处理');
  assert.match(taskSummary({ ...running, completed_pages: 3 }), /3 页/);
});

test('finished exceptional outcomes never claim success even with counts or cache', () => {
  for (const [flag, label] of [['skipped', '已跳过'], ['cancelled', '已取消'], ['stale', '结果已过期'], ['rejected', '发送失败']]) {
    const row = job({ result: { [flag]: true, count: 2, cached: true } });
    assert.equal(taskStatus(row), label);
    assert.doesNotMatch(taskSummary(row), /成功|本次处理|复用/);
  }
});

test('counts, page totals, and translation cache retain their meaning', () => {
  assert.equal(taskSummary(job({ result: { count: 0 } })), '本次处理 0 项');
  assert.equal(taskSummary(job({ result: { count: 2 } })), '本次处理 2 项');
  assert.equal(taskSummary(job({ result: { pages: 3 } })), '本次获取 3 页');
  assert.equal(taskSummary(job({ kind: 'translate_titles', result: { cached: true, count: 0 } })), '已复用现有翻译');
});

test('SMTP handoff does not claim delivery to the inbox', () => {
  assert.equal(taskSummary(job({ kind: 'mail', result: { sent: true } })), '已提交邮件服务器');
});

test('unknown lifecycle never claims success', () => {
  assert.equal(taskStatus(job({ status: 'unknown' })), '未知状态');
  assert.equal(taskSummary(job({ status: 'unknown' })), '暂时无法确认处理结果');
});
