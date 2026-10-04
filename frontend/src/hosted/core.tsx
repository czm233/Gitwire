import { createContext, useContext, ReactNode, useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useLocation } from 'react-router-dom';
import { Card, ErrorBox, ListPager, PageHelp, Loading } from '../components/ui';

export type User = { id: string; login: string; avatar_url: string; email: string; email_verified: boolean; email_enabled: boolean; email_mode: string; timezone: string; digest_hour: number; quiet_start: number; quiet_end: number; notify_kinds: string[]; reconnect_required: boolean; stars_synced_at: number };
export type Auth = { user: User | null; csrf: string | null; github_configured: boolean };
let csrf = '';
export async function request<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await fetch('/api' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json();
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '请求未完成，请检查输入后重试');
  return data;
}
type AuthState = Auth & { loginPending: boolean; startLogin: () => void };
const AuthContext = createContext<AuthState>({ user: null, csrf: null, github_configured: false, loginPending: false, startLogin: () => {} });
export const useAuth = () => useContext(AuthContext);
export function AuthProvider({ children }: { children: ReactNode }) {
  const [loginPending, setLoginPending] = useState(false);
  const navigating = useRef(false);
  useEffect(() => {
    const reset = () => { navigating.current = false; setLoginPending(false); };
    window.addEventListener('pageshow', reset);
    return () => window.removeEventListener('pageshow', reset);
  }, []);
  const query = useQuery<Auth>({ queryKey: ['auth'], queryFn: async () => { const a = await request<Auth>('/auth/me'); csrf = a.csrf || ''; return a; }, retry: false });
  if (query.isPending) return <Loading />;
  if (query.error) return <ErrorBox error={query.error} />;
  const startLogin = () => {
    if (navigating.current || !query.data.github_configured) return;
    navigating.current = true;
    setLoginPending(true);
    window.location.assign('/api/auth/github');
  };
  return <AuthContext.Provider value={{ ...query.data, loginPending, startLogin }}>{children}{loginPending && <div className="host-login-progress" role="status" aria-live="polite"><span className="host-login-spinner" aria-hidden="true" /><strong>正在前往 GitHub…</strong><span>请在 GitHub 确认登录账户</span></div>}</AuthContext.Provider>;
}
export function LoginLink() {
  const auth = useAuth();
  return auth.github_configured ? <button type="button" className="btn" onClick={auth.startLogin} disabled={auth.loginPending}>{auth.loginPending ? '正在前往 GitHub…' : auth.user ? '重新连接 GitHub' : '使用 GitHub 登录'}</button> : <span className="badge" title="管理员配置 GitHub 登录后即可使用个人功能">登录暂不可用</span>;
}
export function SignedIn({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const prompts: Record<string, [string, string]> = {
    '/repos': ['登录后管理监控清单', '保存你关注的仓库，持续跟进 Issue 和项目变化。'],
    '/candidates': ['登录后同步 GitHub 仓库', '从你的 Star 和公开仓库中挑选要监控的项目。'],
    '/runs': ['登录后查看运行历史', '查看与你的监控相关的采集、分析任务和执行结果。'],
    '/daily': ['登录后查看每日晨报', '集中阅读你关注项目的新动态和参与机会。'],
    '/alerts': ['登录后查看个人警报', '查看你选择的监控和提醒规则产生的变化。'],
    '/deliveries': ['登录后查看邮件投递记录', '检查你的提醒邮件是否已发送及失败原因。'],
    '/legacy': ['登录后查看旧版私人资料', '旧版资料仅对所属账户可见。'],
  };
  const [title, description] = prompts[pathname] || ['登录后管理个人设置', '管理 GitHub 连接、邮件提醒和时间偏好。'];
  return useAuth().user ? <>{children}</> : <Card><div className="host-empty"><h2>{title}</h2><p>{description}</p><LoginLink /><p><Link to="/issues">先浏览公开项目 →</Link></p></div></Card>;
}
export function Head({ title, help, children }: { title: string; help: ReactNode; children?: ReactNode }) {
  return <div className="page-head"><div className="page-head-left"><h1 className="page-title">{title}</h1><PageHelp title={title}>{help}</PageHelp></div>{children}</div>;
}
export function useAction(fn: (arg: any) => Promise<any>) {
  const qc = useQueryClient();
  return useMutation({ mutationFn: fn, onSuccess: () => { qc.invalidateQueries(); } });
}
export function usePager() {
  const [page, setPage] = useState(1), [size, setSize] = useState(25);
  const reset = () => setPage(1);
  const view = (data: any) => data && <ListPager page={data.page} pages={data.pages} total={data.total} pageSize={size} onPage={setPage} onPageSize={n => { setSize(n); reset(); }} />;
  return { page, size, reset, view, query: `page=${page}&page_size=${size}` };
}
export const stamp = (n: number) => n ? new Date(n * 1000).toISOString() : '';
