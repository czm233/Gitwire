import { useEffect, useState } from 'react';
import { LogOut, Settings as SettingsIcon, UserRound, CheckCircle2, CircleAlert } from 'lucide-react';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '../components/ui/dropdown-menu';
import { QueryClient, QueryClientProvider, useMutation } from '@tanstack/react-query';
import { BrowserRouter, Link, NavLink, Navigate, Outlet, Route, Routes, useSearchParams } from 'react-router-dom';
import { AuthProvider, request, useAuth } from './core';
import Issues from './Issues';
import Tasks from './Tasks';
import Archives from './Archives';
import { Unsubscribe, Deliveries, LegacyRecords } from './AccountRecords';
import { Candidates, Repos } from './Monitoring';
import { PersonalList, Settings } from './Personal';

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 5000 } } });
function AccountIcon() {
  const auth = useAuth();
  const [failedAvatar, setFailedAvatar] = useState('');
  const logout = useMutation({ mutationFn: async () => {
    await request('/auth/logout', 'POST');
    window.location.replace('/issues?auth=logged-out');
  } });
  if (auth.user) {
    const label = auth.user.reconnect_required ? '账户菜单 · GitHub 需要重新连接' : '账户菜单';
    return <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" className="host-account-icon" aria-label={label} title={label}>
          {auth.user.avatar_url && failedAvatar !== auth.user.avatar_url
            ? <img className="host-avatar" src={auth.user.avatar_url} alt="" draggable={false} referrerPolicy="no-referrer" onError={() => setFailedAvatar(auth.user!.avatar_url)} />
            : <UserRound size={22} aria-hidden="true" />}
          {auth.user.reconnect_required && <span className="host-account-dot" aria-hidden="true" />}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" sideOffset={8} className="host-account-menu" aria-label="账户菜单">
        <DropdownMenuItem asChild className="host-account-menu-item">
          <Link to="/settings" draggable={false}><SettingsIcon size={16} aria-hidden="true" />设置</Link>
        </DropdownMenuItem>
        <DropdownMenuSeparator className="host-account-menu-separator" />
        <DropdownMenuItem className="host-account-menu-item" disabled={logout.isPending} onSelect={event => { event.preventDefault(); logout.mutate(); }}>
          <LogOut size={16} aria-hidden="true" />{logout.isPending ? '正在退出…' : '退出登录'}
        </DropdownMenuItem>
        {logout.error && <p className="host-account-menu-error" role="alert">退出失败，请重试。</p>}
      </DropdownMenuContent>
    </DropdownMenu>;
  }
  const icon = <svg width="24" height="24" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.65 7.65 0 0 1 4 0c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" /></svg>;
  return auth.github_configured
    ? <button type="button" className="host-account-icon" onClick={auth.startLogin} disabled={auth.loginPending} aria-label={auth.loginPending ? "正在前往 GitHub" : "使用 GitHub 登录"} title="使用 GitHub 登录">{icon}</button>
    : <button className="host-account-icon" disabled aria-label="登录暂不可用" title="登录暂不可用">{icon}</button>;
}
function AuthFeedback() {
  const auth = useAuth();
  const [params, setParams] = useSearchParams();
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const result = params.get('auth');
  useEffect(() => {
    if (!result) return;
    const messages: Record<string, string> = {
      success: auth.user ? `登录成功 · ${auth.user.login}` : '',
      'logged-out': auth.user ? '' : '已退出 Gitwire',
      cancelled: '已取消 GitHub 登录',
      expired: '登录请求已过期，请重新登录',
    };
    if (!(result in messages)) return;
    if (messages[result]) setNotice({ text: messages[result], error: result === 'expired' });
    setParams(previous => { const next = new URLSearchParams(previous); next.delete('auth'); return next; }, { replace: true });
  }, [result, auth.user, setParams]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  return notice && <div className="host-auth-notice" role={notice.error ? 'alert' : 'status'}>
    {notice.error ? <CircleAlert size={18} aria-hidden="true" /> : <CheckCircle2 size={18} aria-hidden="true" />}<span>{notice.text}</span>
  </div>;
}

function Shell() {
  const nav = [['/issues', 'Issue 雷达'], ['/repos', '监控清单'], ['/candidates', '候选仓库'], ['/archives', '项目档案'], ['/runs', '运行历史'], ['/daily', '每日晨报'], ['/alerts', '警报'], ['/settings', '设置']];
  return <div className="shell host-shell">
    <header className="host-topbar" aria-label="应用栏">
      <div className="host-brand"><div className="brand">GITWIRE<span className="cursor" /></div></div>
      <div className="host-topbar-main"><div className="brand-sub">OSS INTEL DESK</div><AccountIcon /></div>
    </header>
    <aside className="side">
      <nav className="host-nav" aria-label="主导航">{nav.map(([to, name], i) => <NavLink key={to} to={to} className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`} draggable={false}><span className="idx">{String(i + 1).padStart(2, '0')}</span>{name}</NavLink>)}</nav>
      <div className="side-footer">OBSERVE → ANALYZE → UNDERSTAND<br />关注由你选择 · 情报持续更新</div>
    </aside>
    <div className="host-workspace">
      <AuthFeedback />
      <main className="main"><Outlet /><div className="footer-note">GITWIRE // 开源情报站</div></main>
    </div>
  </div>;
}
export default function HostedApp() {
  return <QueryClientProvider client={client}><AuthProvider><BrowserRouter><Routes><Route element={<Shell />}><Route path="/" element={<Navigate to="/issues" replace />} /><Route path="/issues" element={<Issues />} /><Route path="/repos" element={<Repos />} /><Route path="/candidates" element={<Candidates />} /><Route path="/archives" element={<Archives />} /><Route path="/runs" element={<Tasks />} /><Route path="/daily" element={<PersonalList kind="daily" />} /><Route path="/alerts" element={<PersonalList kind="notifications" />} /><Route path="/settings" element={<Settings />} /><Route path="/unsubscribe" element={<Unsubscribe />} /><Route path="/deliveries" element={<Deliveries />} /><Route path="/legacy" element={<LegacyRecords />} /><Route path="*" element={<Navigate to="/issues" replace />} /></Route></Routes></BrowserRouter></AuthProvider></QueryClientProvider>;
}
