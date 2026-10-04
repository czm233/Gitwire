import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { Card, Confirm, Empty, ErrorBox, Loading, Modal, TimeCell } from '../components/ui';
import { GSelect } from '../components/GSelect';
import { taskStatus, taskSummary } from './taskPresentation';
import { Head, request, useAction, useAuth, usePager, SignedIn, stamp } from './core';

const opportunityFields = [{ key: 'max_age_days', label: '超龄 Issue 天数（且近 14 天无动静）', max: 3650 }, { key: 'max_comments', label: '评论数上限', max: 10000 }, { key: 'repo_pushed_within_days', label: '仓库最近提交天数', max: 3650 }, { key: 'external_merge_within_days', label: '最近外部 PR 合并天数', max: 3650 }];
const recipes = [{ id: 'docs-sync', name: '档案同步' }, { id: 'issue-radar', name: 'Issue 雷达' }, { id: 'feature-tripwire', name: '功能哨兵' }, { id: 'bug-watch', name: '缺陷观察' }, { id: 'cve-scan', name: '依赖漏洞' }];
export function Repos() {
  const auth = useAuth(), [params] = useSearchParams();
  const [add, setAdd] = useState(params.get('add') || ''), [modal, setModal] = useState(!!params.get('add')), [selected, setSelected] = useState(['docs-sync', 'issue-radar']), [remove, setRemove] = useState<any>(null), [editing, setEditing] = useState<any>(null);
  const pg = usePager();
  const query = useQuery({ queryKey: ['repos', auth.user?.id], queryFn: () => request('/repos'), enabled: !!auth.user });
  const create = useAction(async () => { const result = await request('/repos', 'POST', { repos: [add], recipes: selected }); setModal(false); setAdd(''); pg.reset(); return result; });
  const edit = useAction((r: any) => request('/repos/' + r.id, 'PATCH', r.changes));
  const del = useAction(async (id: string) => { await request('/repos/' + id, 'DELETE'); setRemove(null); pg.reset(); });
  useEffect(() => { if (auth.user && params.get('add')) { setAdd(params.get('add')!); setModal(true); } }, [params, auth.user?.id]);
  const rows = query.data?.repos || [], data = { page: pg.page, pages: Math.max(1, Math.ceil(rows.length / pg.size)), total: rows.length };
  return <><Head title="监控清单" help={<><p>每个用户有独立的监控清单。暂停后不再为你生成该项目的监控提醒；移除不会修改 GitHub 数据。</p><p>档案同步维护项目文档，Issue 雷达分析贡献机会；功能哨兵和缺陷观察跟进项目变化，依赖漏洞检查已知漏洞。默认启用档案同步和 Issue 雷达。</p><p>机会筛选按每个仓库独立配置：难度范围、超龄且久未更新、评论过热、仓库近期提交和外部 PR 合并。数值 0 表示关闭对应条件。条件不满足或证据待更新时进入“未入榜”，详情说明原因。</p><p>关闭浏览器不影响服务器运行。仓库转为私有或不可访问后，会停止展示与监控。</p></>}>{auth.user && <button className="btn" onClick={() => { create.reset(); setAdd(''); setSelected(['docs-sync', 'issue-radar']); setModal(true); }}>＋ 添加监控</button>}</Head><SignedIn>
    {(query.error || edit.error || del.error) && <ErrorBox error={query.error || edit.error || del.error} />}
    <Card>{query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : rows.length === 0 ? <Empty text="还没有监控项目。添加仓库，或从候选仓库中挑选。" /> : <div className="host-table-wrap"><table className="tbl"><thead><tr><th>仓库</th><th>监控内容</th><th>状态</th><th>添加时间</th><th>操作</th></tr></thead><tbody>{rows.slice((pg.page - 1) * pg.size, pg.page * pg.size).map((r: any) => <tr key={r.id}><td><a href={'https://github.com/' + r.repo} target="_blank" rel="noreferrer">{r.repo}</a><p className="dim small">{r.description}</p>{!r.public && <p>{r.unavailable_reason}</p>}</td><td>{r.recipes.map((v: string) => recipes.find(x => x.id === v)?.name).join(' · ')}{r.watched_issues.length > 0 && <p className="small">追踪 {r.watched_issues.map((n: number) => '#' + n).join(' · ')}</p>}</td><td className="nw">{!r.public ? '不可访问' : r.paused ? '已暂停' : '监控中'}</td><td><TimeCell iso={stamp(r.created_at)} /></td><td><div className="host-actions"><Link className="btn ghost" to={'/archives?repo=' + encodeURIComponent(r.repo)}>档案</Link><button className="btn ghost" disabled={edit.isPending} onClick={() => edit.mutate({ id: r.id, changes: { paused: !r.paused } })}>{r.paused ? '恢复' : '暂停'}</button><button className="btn ghost" onClick={() => { edit.reset(); setEditing({ ...r, watchText: r.watched_issues.join(', ') }); }}>配置</button><button className="btn ghost" onClick={() => setRemove(r)}>移除</button></div></td></tr>)}</tbody></table></div>}{pg.view(data)}</Card>
    </SignedIn>
    {modal && auth.user && <Modal title="添加监控" onClose={() => setModal(false)}><form onSubmit={e => { e.preventDefault(); create.mutate(null); }}><label className="host-field">公开仓库<input className="input" value={add} onChange={e => setAdd(e.target.value)} placeholder="owner/repo 或 GitHub 地址" required /></label><fieldset><legend>监控内容</legend>{recipes.map(r => <label className="host-check" key={r.id}><input type="checkbox" checked={selected.includes(r.id)} onChange={e => setSelected(e.target.checked ? [...selected, r.id] : selected.filter(v => v !== r.id))} />{r.name}</label>)}</fieldset>{create.error && <ErrorBox error={create.error} />}<button className="btn" disabled={create.isPending || !selected.length}>{create.isPending ? '正在添加…' : '加入监控'}</button></form></Modal>}
    {editing && <Modal title={'监控配置 · ' + editing.repo} onClose={() => setEditing(null)}><fieldset><legend>监控内容</legend>{recipes.map(r => <label className="host-check" key={r.id}><input type="checkbox" checked={editing.recipes.includes(r.id)} onChange={e => setEditing({ ...editing, recipes: e.target.checked ? [...editing.recipes, r.id] : editing.recipes.filter((v: string) => v !== r.id) })} />{r.name}</label>)}</fieldset><label className="host-field">追踪 Issue 编号（逗号或空格分隔）<input className="input" value={editing.watchText} onChange={e => setEditing({ ...editing, watchText: e.target.value })} placeholder="例如 28, 123" /></label><fieldset><legend>新机会提醒的难度范围</legend>{['简单', '中等', '困难'].map(d => <label className="host-check" key={d}><input type="checkbox" checked={editing.difficulty.includes(d)} onChange={e => setEditing({ ...editing, difficulty: e.target.checked ? [...editing.difficulty, d] : editing.difficulty.filter((v: string) => v !== d) })} />{d}</label>)}</fieldset><fieldset><legend>机会筛选条件（0 表示关闭）</legend><div className="host-form-grid">{opportunityFields.map(f => <label className="host-field" key={f.key}>{f.label}<input className="input" type="number" min={0} max={f.max} step={1} value={editing.opportunity[f.key]} onChange={e => setEditing({ ...editing, opportunity: { ...editing.opportunity, [f.key]: Number(e.target.value) } })} /></label>)}</div></fieldset><label className="host-field">提醒关键词（空格分隔，留空不限）<input className="input" value={editing.keywords} onChange={e => setEditing({ ...editing, keywords: e.target.value })} /></label>{edit.error && <ErrorBox error={edit.error} />}<button className="btn" disabled={edit.isPending || !editing.recipes.length} onClick={() => edit.mutate({ id: editing.id, changes: { recipes: editing.recipes, difficulty: editing.difficulty, keywords: editing.keywords, opportunity: editing.opportunity, watched_issues: editing.watchText.trim() ? editing.watchText.trim().split(/[\s,，]+/).map(Number) : [] } }, { onSuccess: () => setEditing(null) })}>保存配置</button></Modal>}
    {remove && <Confirm open title="移除监控" body={`停止监控 ${remove.repo}？不会修改 GitHub 项目。`} onConfirm={() => del.mutate(remove.id)} onClose={() => setRemove(null)} danger />}
  </>;
}

export function CandidateTable({ rows, source, selected, setSelected, ignoring, onIgnore }: {
  rows: any[];
  source: string;
  selected: string[];
  setSelected: (value: string[]) => void;
  ignoring: boolean;
  onIgnore: (row: any) => void;
}) {
  return <div className="host-table-wrap"><table className="tbl host-candidates-table"><thead><tr><th><input aria-label="选择本页未监控项目" type="checkbox" checked={rows.some((r: any) => !r.monitored) && rows.filter((r: any) => !r.monitored).every((r: any) => selected.includes(r.full_name))} onChange={e => setSelected(e.target.checked ? [...new Set([...selected, ...rows.filter((r: any) => !r.monitored).map((r: any) => r.full_name)])] : selected.filter(n => !rows.some((r: any) => r.full_name === n)))} /></th><th>仓库</th><th>状态</th>{source === 'starred' && <th className="host-candidate-time" aria-sort="descending" title="你在 GitHub 点 Star 的时间，最新在前">Star 时间 ↓</th>}<th>操作</th></tr></thead><tbody>{rows.map((r: any) => <tr key={r.id}><td><input aria-label={'选择 ' + r.full_name} type="checkbox" disabled={r.monitored} checked={selected.includes(r.full_name)} onChange={e => setSelected(e.target.checked ? [...selected, r.full_name] : selected.filter(n => n !== r.full_name))} /></td><td><a href={'https://github.com/' + r.full_name} target="_blank" rel="noreferrer">{r.full_name}</a><p className="dim small">{r.description || '暂无描述'}</p></td><td className="nw">{r.monitored ? '已监控' : r.ignored ? '已忽略' : '待选择'}</td>{source === 'starred' && <td>{r.starred_at ? <TimeCell iso={r.starred_at} /> : <span title="GitHub 尚未返回 Star 时间，可重新同步列表">未获取</span>}</td>}<td><button className="btn ghost" disabled={ignoring} onClick={() => onIgnore(r)}>{r.ignored ? '恢复候选' : '忽略'}</button></td></tr>)}</tbody></table></div>;
}

export function CandidateSelectionBar({ count, pending, onAdd, onClear }: {
  count: number;
  pending: boolean;
  onAdd: () => void;
  onClear: () => void;
}) {
  if (!count) return null;
  return <div className="host-selection" role="group" aria-label="已选仓库操作">
    <span>已选 {count} 个仓库（含其他页） · 每次最多 50 个</span>
    <div className="host-selection-actions">
      <button className="btn" disabled={count > 50 || pending} onClick={onAdd}>{pending ? '正在加入…' : '加入监控'}</button>
      <button className="btn ghost" disabled={pending} onClick={onClear}>取消选择</button>
    </div>
  </div>;
}

export function Candidates() {
  const auth = useAuth(), pg = usePager();
  const [source, setSource] = useState('starred'), [ignored, setIgnored] = useState(false), [q, setQ] = useState(''), [selected, setSelected] = useState<string[]>([]);
  const query = useQuery({ queryKey: ['candidates', auth.user?.id, source, ignored, q, pg.page, pg.size], queryFn: () => request(`/candidates?source=${source}&ignored=${ignored}&q=${encodeURIComponent(q)}&${pg.query}`), enabled: !!auth.user, refetchInterval: 5000 });
  const sync = useAction(() => request('/candidates/sync', 'POST', { source }));
  const syncJob = useQuery({ queryKey: ['job', auth.user?.id, sync.data?.job_id], queryFn: () => request('/jobs/' + sync.data.job_id), enabled: !!sync.data?.job_id, refetchInterval: q => ['completed', 'failed'].includes(q.state.data?.status) ? false : 2000 });
  const ignore = useAction((row: any) => request('/candidates/' + row.id, 'PATCH', { ignored: !row.ignored }));
  const importRows = useAction(async () => { const result = await request('/repos', 'POST', { repos: selected, recipes: ['docs-sync', 'issue-radar'] }); setSelected([]); return result; });
  return <><Head title="候选仓库" help={<><p>我的 Star 和我的公开仓库来自 GitHub 只读授权。新增 Star 每天检查，也可以手动刷新。</p><p>同步只更新候选列表，不会自动开启监控；忽略状态会保留。加入监控默认启用档案同步和 Issue 雷达。</p><p>Star 时间来自 GitHub，表示你给仓库点 Star 的时间，按最新在前排列，并按当前设备时区显示。未获取到时间的记录排在最后，可重新同步；不会用导入时间代替。我的公开仓库不显示 Star 时间。</p><p>一次最多添加 50 个仓库，重复选择已监控项目不会重复创建。</p></>}>{auth.user && <button className="btn" disabled={sync.isPending || (!!sync.data?.job_id && !syncJob.error && !['completed', 'failed'].includes(syncJob.data?.status))} onClick={() => sync.mutate(null)}>{sync.isPending || ['pending', 'running'].includes(syncJob.data?.status) ? '正在同步…' : '同步 GitHub 列表'}</button>}</Head><SignedIn>
    {(query.error || sync.error || ignore.error || importRows.error) && <ErrorBox error={query.error || sync.error || ignore.error || importRows.error} />}
    {sync.isSuccess && <p role="status">{syncJob.data ? `同步状态：${taskStatus(syncJob.data)}。${taskSummary(syncJob.data)}` : '同步任务已排队，正在获取进度。'} <Link to="/runs">查看运行历史</Link></p>}{syncJob.error && <ErrorBox error={syncJob.error} />}
    {importRows.isSuccess && <p role="status">已加入监控，可前往监控清单配置。</p>}
    <Card><div className="host-row host-filters"><GSelect ariaLabel="仓库来源" value={source} options={[{ value: 'starred', label: '我的 Star' }, { value: 'owned', label: '我的公开仓库' }]} onChange={v => { setSource(v); setSelected([]); pg.reset(); }} /><GSelect ariaLabel="候选范围" value={ignored ? 'ignored' : 'normal'} options={[{ value: 'normal', label: '候选列表' }, { value: 'ignored', label: '已忽略' }]} onChange={v => { setIgnored(v === 'ignored'); setSelected([]); pg.reset(); }} /><input className="input" aria-label="搜索候选仓库" placeholder="搜索仓库" value={q} onChange={e => { setQ(e.target.value); setSelected([]); pg.reset(); }} />{q && <button className="btn ghost" onClick={() => { setQ(''); setSelected([]); pg.reset(); }}>清除搜索</button>}</div><CandidateSelectionBar count={selected.length} pending={importRows.isPending} onAdd={() => importRows.mutate(null)} onClear={() => setSelected([])} />
    {query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : !query.data?.items.length ? <Empty text={q ? "没有匹配的仓库，可以清除搜索后重试。" : ignored ? "没有已忽略的仓库。" : "还没有候选仓库，点击右上角同步 GitHub 列表。"} /> : <CandidateTable rows={query.data.items} source={source} selected={selected} setSelected={setSelected} ignoring={ignore.isPending} onIgnore={r => ignore.mutate(r)} />}{pg.view(query.data)}</Card></SignedIn></>;
}
