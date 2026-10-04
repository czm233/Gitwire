import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { Card, Empty, ErrorBox, Loading, Modal, TimeCell } from '../components/ui';
import { GSelect } from '../components/GSelect';
import IssueDetail from './IssueDetail';
import { Head, request, useAction, useAuth, usePager } from './core';

export default function Issues() {
  const auth = useAuth();
  const [input, setInput] = useState(''), [repo, setRepo] = useState(''), [q, setQ] = useState(''), [group, setGroup] = useState('all'), [difficulty, setDifficulty] = useState(''), [detail, setDetail] = useState<any>(null);
  const [adding, setAdding] = useState(false);
  const submitting = useRef(false);
  const pg = usePager();
  const search = useAction(async () => { const result = await request('/explore?repo=' + encodeURIComponent(input.trim())); setRepo(result.repo.full_name); setGroup('all'); setDifficulty(''); setQ(''); pg.reset(); setAdding(false); return result; });
  const query = useQuery({ queryKey: ['issues', auth.user?.id, repo, q, group, difficulty, pg.page, pg.size], queryFn: () => request(`/issues?repo=${encodeURIComponent(repo)}&q=${encodeURIComponent(q)}&group=${group}&difficulty=${encodeURIComponent(difficulty)}&${pg.query}`), refetchInterval: 10000 });
  const watch = useAction(async (issue: any) => {
    const { repos } = await request('/repos');
    const sub = repos.find((s: any) => s.id === issue.subscription_id);
    const numbers = issue.watched ? sub.watched_issues.filter((n: number) => n !== issue.number) : [...sub.watched_issues, issue.number];
    return request('/repos/' + sub.id, 'PATCH', { watched_issues: numbers });
  });
  const analyze = useAction((id: string) => request('/issues/' + id + '/analyze', 'POST'));
  const filtered = group !== 'all' || !!difficulty || !!q;
  const clearFilters = () => { setGroup('all'); setDifficulty(''); setQ(''); pg.reset(); };
  const showResults = !!auth.user || !!repo;
  const options = [{ value: 'all', label: '全部' }, { value: 'opportunity', label: '可上手' }, { value: 'taken', label: '有人做了' }, { value: 'closed', label: '已关闭' }, { value: 'excluded', label: '未入榜' }, { value: 'pending', label: '待分析' }, { value: 'watched', label: '追踪中' }];
  return <>
    <Head title="Issue 雷达" help={<><p>点击右上角“添加项目”，粘贴公开仓库地址即可查询。登录后可以保存监控，并追踪具体 Issue。</p><p>有人做了的依据包括 GitHub 指派、当前仍开放且明确声明修复本 Issue 的 PR，以及 AI 从新增评论识别的自愿认领。裸引用不算认领，评论认领 14 天后过期。PR 关闭但 Issue 仍开放时重新检查机会；“关闭操作人”不等同于修复代码作者。</p><p>标题下方是原始标题的中文翻译，不是问题摘要；尚未翻译时仅显示原文。问题摘要和分析在详情中查看。</p><p>难度为 AI 估计，不代表实际工作量。点击详情查看分析依据。已关闭与已完成分别显示；出现关联 PR 不代表已经合并。</p><p>可上手须同时满足你配置的难度、Issue 时效、评论量和仓库活跃条件。“未入榜”的具体原因在详情的“机会判断依据”中显示；证据未完成采集时也不直接判为机会。详情提供最近提交、外部 PR 合并和采集时间。阈值在监控清单的设置里修改，0 表示关闭。</p><p>公开数据与通用分析可以复用；你的监控、追踪和提醒设置仅自己可见。页面按创建时间最新在前。</p></>}><button className="btn" onClick={() => { search.reset(); setInput(''); setAdding(true); }}>＋ 添加项目</button></Head>
    {adding && <Modal title="添加项目" onClose={() => { if (!submitting.current) setAdding(false); }}><form onSubmit={e => {
      e.preventDefault();
      if (submitting.current || !input.trim()) return;
      submitting.current = true;
      search.mutate(null, { onSettled: () => { submitting.current = false; } });
    }}>
      <label className="host-field" htmlFor="repo-search">公开仓库<input className="input" id="repo-search" placeholder="owner/repo 或 GitHub 仓库地址" value={input} onChange={e => setInput(e.target.value)} disabled={search.isPending} required /></label>
      <p className="dim small">查询公开 Issue；需要持续跟进时，可再加入监控。</p>
      {search.error && <ErrorBox error={search.error} />}
      <div className="host-actions"><button type="button" className="btn ghost" disabled={search.isPending} onClick={() => setAdding(false)}>取消</button><button className="btn" disabled={search.isPending || !input.trim()}>{search.isPending ? '正在查询…' : '查询项目'}</button></div>
    </form></Modal>}
    {(query.error || watch.error || analyze.error) && <ErrorBox error={query.error || watch.error || analyze.error} />}
    {search.data?.refreshing && query.data?.total === 0 && <p role="status">正在获取项目的 Issue，列表将自动刷新。</p>}
    {showResults && <><div className="host-stats">{options.slice(0, 6).map(o => <button key={o.value} className={`host-stat ${group === o.value ? 'active' : ''}`} onClick={() => { setGroup(o.value); pg.reset(); }}><strong>{query.data?.stats?.[o.value] ?? '—'}</strong><span>{o.label}</span></button>)}</div>
    <Card><div className="host-row host-filters">{repo && <span className="badge">{repo}</span>}{repo && auth.user && <button className="btn ghost" onClick={() => { setRepo(''); setInput(''); clearFilters(); }}>我的监控</button>}<GSelect ariaLabel="Issue 分组" value={group} onChange={v => { setGroup(v); pg.reset(); }} options={options} /><GSelect ariaLabel="Issue 难度" value={difficulty} placeholder="全部难度" options={['简单', '中等', '困难', '未分析'].map(value => ({ value, label: value }))} onChange={v => { setDifficulty(v); pg.reset(); }} /><input className="input" aria-label="筛选 Issue" placeholder="筛选原文或中文标题" value={q} onChange={e => { setQ(e.target.value); pg.reset(); }} />{filtered && <button className="btn ghost" onClick={clearFilters}>清除筛选</button>}{repo && auth.user && <Link className="btn ghost" to={'/repos?add=' + encodeURIComponent(repo)}>加入监控</Link>}</div>
      {query.isPending ? <Loading /> : query.isError ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : !query.data?.items.length ? <Empty text={filtered ? '没有符合当前筛选条件的 Issue，可以清除筛选后重试。' : repo ? (search.data?.refreshing ? '正在采集项目数据，请稍候。' : '这个项目暂时没有可显示的 Issue。') : '监控清单中暂无 Issue，可点击右上角“添加项目”。'} /> : <div className="host-table-wrap"><table className="tbl host-issues-table"><thead><tr><th>Issue</th><th>仓库</th><th>难度</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{query.data.items.map((i: any) => <tr key={i.id}><td><a href={i.url} target="_blank" rel="noreferrer">#{i.number} {i.title}</a>{i.title_zh && i.title_zh !== i.title && <p className="host-issue-translation" lang="zh-CN">{i.title_zh}</p>}</td><td className="host-issue-repo"><Link to={'/archives?repo=' + encodeURIComponent(i.repo)}>{i.repo}</Link></td><td className="nw" title="AI 基于公开内容评估，点详情查看依据">{i.difficulty}</td><td className="nw">{{ open: '开放', 'taken-pr': '有关联 PR', 'taken-assignee': '已指派', 'taken-claim': '评论认领', resolved: '已标记完成', closed: '已关闭' }[i.status as string] || i.status}</td><td><TimeCell iso={i.created_at} /></td><td><div className="host-actions"><button className="btn ghost" onClick={() => setDetail(i)}>详情</button>{i.subscription_id && <button className="btn ghost" disabled={watch.isPending} onClick={() => watch.mutate(i)}>{i.watched ? '取消追踪' : '追踪'}</button>}{auth.user && i.difficulty === '未分析' && <button className="btn ghost" disabled={analyze.isPending} onClick={() => analyze.mutate(i.id)}>分析</button>}</div></td></tr>)}</tbody></table></div>}{pg.view(query.data)}</Card></>}{!showResults && <Card><div className="host-empty"><h2>查看公开项目的 Issue</h2><p>点击右上角“添加项目”，输入仓库地址即可查看，无需登录。</p></div></Card>}
    {analyze.isSuccess && <p role="status">分析任务已排队，可在运行历史查看。</p>}
    {detail && <IssueDetail key={detail.id} issue={detail} onClose={() => setDetail(null)} />}
  </>;
}
