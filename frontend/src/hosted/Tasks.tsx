import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Card, Empty, ErrorBox, Loading, Modal, TimeCell } from '../components/ui';
import { GSelect } from '../components/GSelect';
import { Head, request, SignedIn, stamp, useAction, useAuth, usePager } from './core';
import { taskStates, taskStatus, taskSummary } from './taskPresentation';

export const taskKinds: Record<string, string> = { translate_titles: '翻译 Issue 标题', analyze_claim: '判断评论认领', repo_signals: '检查项目活跃证据', scan: '更新仓库动态', candidates: '同步候选仓库', analyze_issue: '分析 Issue', analyze_repo: '更新项目档案', watch_issues: '追踪 Issue 动态', digest: '生成晨报', mail: '发送邮件' };

export function TaskTable({ rows, onDetail }: { rows: any[]; onDetail: (id: string) => void }) {
  return <div className="host-table-wrap"><table className="tbl host-runs-table">
    <thead><tr><th>任务类型</th><th>仓库</th><th>处理结果</th><th>处理状态</th><th>尝试次数</th><th>创建时间</th><th>操作</th></tr></thead>
    <tbody>{rows.map(row => <tr key={row.id}>
      <td className="host-run-kind">{taskKinds[row.kind] || row.kind}</td>
      <td className="host-run-repo">{row.repo ? <Link to={'/archives?repo=' + encodeURIComponent(row.repo)}>{row.repo}</Link> : '—'}</td>
      <td className="host-run-result">{taskSummary(row)}</td>
      <td className="nw">{taskStatus(row)}</td>
      <td className="nw">{row.attempts} 次</td>
      <td><TimeCell iso={stamp(row.created_at)} /></td>
      <td><button className="btn ghost" onClick={() => onDetail(row.id)}>详情</button></td>
    </tr>)}</tbody>
  </table></div>;
}

export default function Tasks() {
  const auth = useAuth(), pg = usePager();
  const [status, setStatus] = useState(''), [kind, setKind] = useState(''), [opened, setOpened] = useState('');
  const query = useQuery({ queryKey: ['jobs', auth.user?.id, status, kind, pg.page, pg.size], queryFn: () => request(`/jobs?status=${status}&kind=${kind}&${pg.query}`), enabled: !!auth.user, refetchInterval: 5000 });
  const detail = useQuery({ queryKey: ['job', auth.user?.id, opened], queryFn: () => request('/jobs/' + opened), enabled: !!auth.user && !!opened, refetchInterval: 5000 });
  const retry = useAction(async (id: string) => { const result = await request(`/jobs/${id}/retry`, 'POST'); setOpened(result.job_id); return result; });
  return <><Head title="运行历史" help={<><p>显示你的账户任务和与你的监控有关的公开项目任务。多人监控同一项目时，服务器会合并采集和通用分析，个人候选、邮件和晨报任务只对本人可见。</p><p>任务类型、仓库、处理结果、处理状态、尝试次数与创建时间分列显示；“—”表示没有关联仓库。针对单条 Issue 的任务，可在详情中查看关联 Issue。</p><p>处理状态明确区分成功、失败、跳过、取消和结果过期；追踪 Issue 成功表示检查完成，不代表发现了变化。统计与筛选按任务执行阶段分组，“已结束”包含成功、跳过、取消、结果过期及邮件拒收，具体结果以行内处理状态为准。</p><p>等待中包括排队、额度不足及重试等待；点详情查看下次可执行时间。运行中有租约保护，进程意外退出后由其他 Worker 接手。</p><p>失败任务可手动重试，保留原失败记录并创建或合并新任务。已暂停、已移除或不公开的项目会重新检查是否允许执行。邮件须到投递记录中重试。</p></>} /><SignedIn>
    {(query.error || retry.error) && <ErrorBox error={query.error || retry.error} />}
    <div className="host-stats">{Object.entries(taskStates).map(([value, label]) => <button key={value} className={'host-stat ' + (status === value ? 'active' : '')} onClick={() => { setStatus(status === value ? '' : value); pg.reset(); }}><strong>{query.data?.stats?.[value] ?? '—'}</strong><span>{label}</span></button>)}</div>
    <Card><div className="host-row host-filters"><GSelect value={status} placeholder="全部状态" options={Object.entries(taskStates).map(([value, label]) => ({ value, label }))} onChange={v => { setStatus(v); pg.reset(); }} /><GSelect value={kind} placeholder="全部任务" options={Object.entries(taskKinds).map(([value, label]) => ({ value, label }))} onChange={v => { setKind(v); pg.reset(); }} />{(status || kind) && <button className="btn ghost" onClick={() => { setStatus(''); setKind(''); pg.reset(); }}>清除筛选</button>}</div>
      {query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : !query.data?.items.length ? <Empty text="暂无符合条件的任务" /> : <TaskTable rows={query.data.items} onDetail={setOpened} />}{pg.view(query.data)}</Card>
      {opened && <Modal title={taskKinds[detail.data?.kind] || '任务详情'} onClose={() => setOpened('')}>{detail.error ? <ErrorBox error={detail.error} /> : detail.isPending ? <Loading /> : <>
        <p>{detail.data.repo}</p>
        {detail.data.repo && detail.data.issue_number && <p>关联 Issue：<a href={`https://github.com/${detail.data.repo}/issues/${detail.data.issue_number}`} target="_blank" rel="noreferrer">#{detail.data.issue_number}</a></p>}
        <p>处理状态：{taskStatus(detail.data)} · 已尝试 {detail.data.attempts} / {detail.data.max_attempts} 次</p><p>{taskSummary(detail.data)}</p>
        {detail.data.status === 'pending' && <p>最早可执行时间：{new Date(detail.data.available_at * 1000).toLocaleString()}（仍需等待 Worker 空闲）</p>}
        {detail.data.finished_at > 0 && <p>结束时间：{new Date(detail.data.finished_at * 1000).toLocaleString()}</p>}
        {retry.error && <ErrorBox error={retry.error} />}{detail.data.kind === 'mail' ? <Link className="btn ghost" to="/deliveries">查看投递记录</Link> : detail.data.status === 'failed' && <button className="btn" disabled={retry.isPending} onClick={() => retry.mutate(opened)}>重试任务</button>}
      </>}</Modal>}
    </SignedIn></>;
}
