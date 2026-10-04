type Task = {
  kind: string;
  status: string;
  error?: string;
  completed_pages?: number;
  result?: {
    count?: number;
    pages?: number;
    cached?: boolean;
    skipped?: boolean;
    stale?: boolean;
    sent?: boolean;
    cancelled?: boolean;
    rejected?: boolean;
  };
};

// Filter buckets describe worker lifecycle; individual rows also show business outcomes.
export const taskStates: Record<string, string> = { pending: '等待中', running: '运行中', completed: '已结束', failed: '执行失败' };

export function taskStatus(job: Task): string {
  if (job.status === 'failed') return '失败';
  if (job.status !== 'completed') return taskStates[job.status] || '未知状态';
  if (job.result?.rejected) return '发送失败';
  if (job.result?.cancelled) return '已取消';
  if (job.result?.skipped) return '已跳过';
  if (job.result?.stale) return '结果已过期';
  return '成功';
}

export function taskSummary(job: Task): string {
  // Pending/running jobs may retain an error from an earlier attempt or deferral.
  if (job.status === 'failed') return job.error || '处理失败，请查看详情或重试';
  if (job.status === 'pending') return job.error ? `等待继续处理：${job.error}` : '已排队，等待处理';
  if (job.status === 'running') return job.completed_pages
    ? `已完成 ${job.completed_pages} 页，正在获取后续内容` : '正在处理';
  if (job.status !== 'completed') return '暂时无法确认处理结果';
  const result = job.result || {};
  if (result.rejected) return '邮件服务器拒绝收件地址，请到投递记录查看';
  if (result.cancelled) return '已取消，本次未发送';
  if (result.skipped) return '不满足执行条件，本次已跳过';
  if (result.stale) return '内容已变化，本次结果未采用';
  if (result.sent) return '已提交邮件服务器';
  if (result.cached) return job.kind === 'translate_titles' ? '已复用现有翻译' : '已复用现有分析';
  if (result.count !== undefined) return `本次处理 ${result.count} 项`;
  if (result.pages !== undefined) return `本次获取 ${result.pages} 页`;
  if (job.kind === 'watch_issues') return '已完成 Issue 动态检查';
  return '处理成功';
}
