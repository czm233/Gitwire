import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { Card, Empty, ErrorBox, Loading, Modal, TimeCell } from '../components/ui';
import { GSelect } from '../components/GSelect';
import Markdown from '../components/Markdown';
import { Head, request, stamp, useAuth, usePager } from './core';

export default function Archives() {
  const auth = useAuth();
  const [params, setParams] = useSearchParams();
  const repo = params.get('repo') || '';
  const [input, setInput] = useState(repo), [path, setPath] = useState(''), [opened, setOpened] = useState(''), [diff, setDiff] = useState(false);
  const pg = usePager();
  const query = useQuery({ queryKey: ['artifacts', repo, path, pg.page, pg.size],
    queryFn: () => request(`/artifacts?repo=${encodeURIComponent(repo)}&path=${encodeURIComponent(path)}&${pg.query}`), enabled: !!repo });
  const detail = useQuery({ queryKey: ['artifact', opened], queryFn: () => request('/artifacts/' + opened), enabled: !!opened });
  return <>
    <Head title="项目档案" help={<><p>这里保存公开项目的分析文档及历史版本。监控清单启用“档案同步”后，服务器会在项目变化时更新文档。</p><p>每次正文变化保留一个版本，最新版本排在最前。点“阅读”查看内容，点“对比”查看相对上一版新增与删除的文字。AI 分析可能有误，应结合原始项目核实。</p><p>公开档案可以共享阅读。自己的监控规则和日报不在这里展示；需要保存到本地时，在设置中下载个人情报。</p></>} />
    <form className="host-search" onSubmit={e => { e.preventDefault(); setParams({ repo: input.trim() }); setPath(''); pg.reset(); }}>
      <label className="host-search-label" htmlFor="archive-repo">查看一个项目的积累</label>
      <div className="host-row"><input className="input" id="archive-repo" value={input} onChange={e => setInput(e.target.value)} placeholder="owner/repo 或 GitHub 仓库地址" required /><button className="btn">查看档案</button>{auth.user && repo && <Link className="btn ghost" to="/repos">前往监控清单</Link>}</div>
    </form>
    {query.error && <ErrorBox error={query.error} />}
    <Card>
      {!repo ? <Empty text="从 Issue 的项目名称或监控清单进入，也可以直接输入仓库地址。" /> : query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : <>
        {!!query.data?.paths.length && <div className="host-row host-filters"><GSelect value={path} onChange={v => { setPath(v); pg.reset(); }} placeholder="全部文档" options={query.data.paths.map((p: string) => ({ value: p, label: p }))} /><span>{query.data.repo}</span></div>}
        {!query.data?.items.length ? <Empty text={auth.user ? "暂无已生成的档案。可在监控清单启用档案同步，在运行历史查看进度。" : "这个项目暂无公开档案。登录后可将项目加入监控，并启用档案同步。"} /> : <div className="host-table-wrap"><table className="tbl"><thead><tr><th>文档</th><th>版本</th><th>生成时间</th><th>操作</th></tr></thead><tbody>{query.data.items.map((row: any) => <tr key={row.id}><td>{row.path}</td><td className="nw"><code>{row.revision.slice(0, 12)}</code></td><td><TimeCell iso={stamp(row.created_at)} /></td><td><div className="host-actions"><button className="btn ghost" onClick={() => { setOpened(row.id); setDiff(false); }}>阅读</button><button className="btn ghost" onClick={() => { setOpened(row.id); setDiff(true); }}>对比</button></div></td></tr>)}</tbody></table></div>}
        {pg.view(query.data)}
      </>}
    </Card>
    {opened && <Modal title={detail.data?.path || '项目档案'} onClose={() => setOpened('')}>
      <div className="host-row"><button className={'btn ' + (diff ? 'ghost' : '')} onClick={() => setDiff(false)}>阅读正文</button><button className={'btn ' + (diff ? '' : 'ghost')} onClick={() => setDiff(true)}>与上一版对比</button></div>
      {detail.error ? <ErrorBox error={detail.error} /> : detail.isPending ? <Loading /> : diff ? <><p className="small">{detail.data.previous_id ? `${detail.data.previous_revision.slice(0, 12)} → ${detail.data.revision.slice(0, 12)}` : '这是首个版本，全部内容均为新增。'}</p><pre className="host-pre">{detail.data.diff || '正文没有变化。'}</pre></> : <Markdown text={detail.data.content} />}
    </Modal>}
  </>;
}
