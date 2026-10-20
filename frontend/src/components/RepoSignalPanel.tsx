import { useQuery } from "@tanstack/react-query";
import { request } from "../hosted/core";

const fmtStars = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));
const ago = (unix: number) => {
  if (!unix) return "尚无记录";
  const diff = Math.max(0, Date.now() / 1000 - unix);
  if (diff < 3600) return `${Math.max(1, Math.round(diff / 60))} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  return `${Math.round(diff / 86400)} 天前`;
};

/** 雷达右栏「仓库信号面板」（V04 原型 index.html 右栏）：
 * 数据来自 /repos 微调后的只读字段（stars / pushed_at / external_merge_at），
 * 不渲染原型中的健康度进度条——无真实语义，不做假指标（CZM-33 方案三）。 */
export default function RepoSignalPanel() {
  const query = useQuery({
    queryKey: ["repo-signals"],
    queryFn: () => request("/repos") as Promise<{ repos: any[] }>,
    refetchInterval: 60000,
    select: (data) => (data?.repos || []).slice(0, 8),
  });
  const repos = query.data || [];
  if (query.isError || !repos.length) return null;
  return (
    <aside className="card" aria-label="仓库信号面板">
      <div className="card-h">
        <span className="card-t">仓库信号面板 <span className="cnt mono">{repos.length} 仓库</span></span>
      </div>
      {repos.map((r: any) => (
        <div className="sig" key={r.id}>
          <div className="sig-top">
            <span className="sig-name mono">{r.repo}</span>
            <span className="sig-stars mono" title="GitHub Stars">★ <b>{fmtStars(r.stars ?? 0)}</b></span>
          </div>
          <div className="sig-m">
            <span>最近推送 <b className="mono">{ago(r.pushed_at)}</b></span>
            <span>外部 PR 合并 <b className="mono">{ago(r.external_merge_at)}</b></span>
          </div>
        </div>
      ))}
    </aside>
  );
}
