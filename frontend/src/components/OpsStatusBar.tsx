import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { request, useAuth } from "../hosted/core";

/** 航情台底部状态条（V04 Ops Deck 签名元素）：
 * 左侧 LIVE 信号灯 + 登录态任务统计（/jobs stats，真实数据），
 * 右侧上次数据更新（react-query 缓存 dataUpdatedAt，真实）+ 本机时钟。
 * 纯环境信息，屏幕阅读器跳过（时钟每秒跳动会刷屏）。 */
export default function OpsStatusBar() {
  const auth = useAuth();
  const qc = useQueryClient();
  const [now, setNow] = useState(() => new Date());
  const [updated, setUpdated] = useState(0);
  useEffect(() => {
    const tick = () => {
      setNow(new Date());
      let latest = 0;
      for (const query of qc.getQueryCache().getAll()) {
        if (query.state.dataUpdatedAt > latest) latest = query.state.dataUpdatedAt;
      }
      setUpdated(latest);
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [qc]);
  const jobs = useQuery({
    queryKey: ["ops-jobs", auth.user?.id],
    queryFn: () => request("/jobs?page=1&page_size=1"),
    enabled: !!auth.user,
    refetchInterval: 30000,
    select: (data: any) => data?.stats as Record<string, number> | undefined,
  });
  const p = (n: number) => String(n).padStart(2, "0");
  const clock = `${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
  const updatedText = updated
    ? `${p(new Date(updated).getHours())}:${p(new Date(updated).getMinutes())}:${p(new Date(updated).getSeconds())}`
    : "—";
  const stats = jobs.data;
  return (
    <div className="opsbar" aria-hidden="true">
      <span className="grp">
        <span className="pdot" />
        LIVE
        {stats && (stats.running > 0 || stats.pending > 0) && (
          <>
            <span className="sep">·</span>
            任务 <b>运行 {stats.running ?? 0}</b>
            <span className="sep">·</span>
            <b>等待 {stats.pending ?? 0}</b>
          </>
        )}
      </span>
      <span className="grp">
        数据更新 <b>{updatedText}</b>
        <span className="sep">·</span>
        {clock}
      </span>
    </div>
  );
}
