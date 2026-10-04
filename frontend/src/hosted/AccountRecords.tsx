import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router-dom';
import { Card, Empty, ErrorBox, Loading, Modal, TimeCell } from '../components/ui';
import { GSelect } from '../components/GSelect';
import Markdown from '../components/Markdown';
import { Head, request, SignedIn, stamp, useAction, useAuth, usePager } from './core';

const deliveryStates: Record<string, string> = { pending: '等待发送', sent: '已交给邮件服务器', failed: '发送失败', cancelled: '已取消', bounced: '退信', complained: '收件人投诉' };
const legacyKinds: Record<string, string> = { document: '旧版文档', daily: '旧版晨报', configuration: '旧版配置', state: '分析状态', run: '运行记录', alert: '警报记录' };

export function Unsubscribe() {
  const [params] = useSearchParams();
  const token = params.get('token') || '';
  const stop = useAction(() => request('/email/unsubscribe', 'POST', { token }));
  return <><Head title="停止邮件提醒" help={<><p>此链接只管理邮件提醒，不会修改 GitHub 数据，也不会移除监控。</p><p>打开页面不会退订，点击确认后才生效。以后可在设置中重新开启。</p></>} /><Card>
    {stop.isSuccess ? <div role="status"><h2>退订请求已处理</h2><p>此链接对应邮箱仍是当前收件邮箱时，邮件提醒已关闭，尚未发送的提醒已取消。</p><Link className="btn ghost" to="/settings">返回设置</Link></div> : !token ? <Empty text="缺少退订链接。请从邮件底部重新打开，或登录后在设置中关闭邮件提醒。" /> : <><p>停止接收此邮箱的 Gitwire 提醒？监控和站内情报会继续保留。</p><button className="btn" disabled={stop.isPending} onClick={() => stop.mutate(null)}>{stop.isPending ? '正在处理…' : '确认停止邮件提醒'}</button>{stop.error && <ErrorBox error={stop.error} />}</>}
  </Card></>;
}

export function Deliveries() {
  const auth = useAuth(), pg = usePager();
  const [status, setStatus] = useState('');
  const query = useQuery({ queryKey: ['deliveries', auth.user?.id, status, pg.page, pg.size], queryFn: () => request(`/deliveries?status=${status}&${pg.query}`), enabled: !!auth.user, refetchInterval: 10000 });
  const retry = useAction((id: string) => request(`/deliveries/${id}/retry`, 'POST'));
  return <><Head title="邮件投递记录" help={<><p>只显示你自己的邮件记录。“已交给邮件服务器”表示 SMTP 接收成功，不能保证已经进入收件箱。</p><p>等待发送可能处于免打扰时段或发送额度已用完。提醒过时、已读、邮箱改变或关闭提醒后，待发邮件会取消。</p><p>发送失败可以重试，系统会再次检查提醒是否仍有效。验证邮件过期时请返回设置重新发送。退信或投诉会关闭提醒，需要重新验证邮箱后手动开启。</p></>}><Link className="btn ghost" to="/settings">返回设置</Link></Head><SignedIn>
    {(query.error || retry.error) && <ErrorBox error={query.error || retry.error} />}{retry.isSuccess && <p role="status">已重新加入发送队列。</p>}
    <Card><div className="host-row host-filters"><GSelect value={status} placeholder="全部状态" options={Object.entries(deliveryStates).map(([value, label]) => ({ value, label }))} onChange={v => { setStatus(v); pg.reset(); }} />{status && <button className="btn ghost" onClick={() => { setStatus(''); pg.reset(); }}>清除筛选</button>}</div>
      {query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : !query.data?.items.length ? <Empty text={status ? "暂无符合当前筛选条件的邮件，可以清除筛选后重试。" : "暂无邮件投递记录。验证邮件和已开启的提醒邮件会显示在这里。"} /> : <div className="host-table-wrap"><table className="tbl"><thead><tr><th>邮件</th><th>收件邮箱</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{query.data.items.map((row: any) => <tr key={row.id}><td><strong>{row.subject}</strong>{row.error && <p className="small">{row.error}</p>}</td><td>{row.recipient}</td><td className="nw">{deliveryStates[row.status] || row.status}</td><td><TimeCell iso={stamp(row.created_at)} /></td><td>{row.status === 'failed' ? <button className="btn ghost" disabled={retry.isPending} onClick={() => retry.mutate(row.id)}>重试</button> : '—'}</td></tr>)}</tbody></table></div>}{pg.view(query.data)}
    </Card></SignedIn></>;
}

export function LegacyRecords() {
  const auth = useAuth(), pg = usePager();
  const [kind, setKind] = useState(''), [q, setQ] = useState(''), [opened, setOpened] = useState('');
  const query = useQuery({ queryKey: ['legacy', auth.user?.id, kind, q, pg.page, pg.size], queryFn: () => request(`/legacy?kind=${kind}&q=${encodeURIComponent(q)}&${pg.query}`), enabled: !!auth.user });
  const detail = useQuery({ queryKey: ['legacy-detail', auth.user?.id, opened], queryFn: () => request('/legacy/' + opened), enabled: !!auth.user && !!opened });
  return <><Head title="旧版私人资料" help={<><p>这里保存明确迁移到你账号下的旧版文档、晨报、配置及历史记录，仅你可见。</p><p>旧资料保留迁移时的内容，不会作为共享项目档案公开，也不会随着新分析被覆盖。旧版配置是历史备份，查看它不会修改当前监控设置。</p><p>设置中的“下载我的情报”会包含这些资料，方便保存到本地。</p></>}><Link className="btn ghost" to="/settings">返回设置</Link></Head><SignedIn>
    {query.error && <ErrorBox error={query.error} />}<Card><div className="host-row host-filters"><GSelect value={kind} placeholder="全部类型" options={Object.entries(legacyKinds).map(([value, label]) => ({ value, label }))} onChange={v => { setKind(v); pg.reset(); }} /><input className="input" aria-label="搜索旧版资料" placeholder="搜索资料标题" value={q} onChange={e => { setQ(e.target.value); pg.reset(); }} />{(kind || q) && <button className="btn ghost" onClick={() => { setKind(''); setQ(''); pg.reset(); }}>清除筛选</button>}</div>
      {query.isPending ? <Loading /> : query.isError && !query.data ? <button className="btn ghost" onClick={() => query.refetch()}>重新加载</button> : !query.data?.items.length ? <Empty text={kind || q ? "没有匹配的资料，可以清除筛选后重试。" : "暂无迁移到此账号的旧版资料"} /> : <div className="host-table-wrap"><table className="tbl"><thead><tr><th>资料</th><th>类型</th><th>原记录时间</th><th>操作</th></tr></thead><tbody>{query.data.items.map((row: any) => <tr key={row.id}><td>{row.title}{row.repo && <p className="small">{row.repo}</p>}</td><td className="nw">{legacyKinds[row.kind] || row.kind}</td><td><TimeCell iso={stamp(row.created_at)} /></td><td><button className="btn ghost" onClick={() => setOpened(row.id)}>阅读</button></td></tr>)}</tbody></table></div>}{pg.view(query.data)}</Card>
      {opened && <Modal title={detail.data?.title || '旧版私人资料'} onClose={() => setOpened('')}>{detail.error ? <ErrorBox error={detail.error} /> : detail.isPending ? <Loading /> : ['document', 'daily'].includes(detail.data.kind) ? <Markdown text={detail.data.content} /> : <pre className="host-pre">{detail.data.content}</pre>}</Modal>}
    </SignedIn></>;
}
