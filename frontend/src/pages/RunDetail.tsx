import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import type { RunDetail as RunDetailDTO, RunLog } from "../types";
import { Badge, Card, ErrorBox, Loading, ModeTag, Sha, fmtTime } from "../components/ui";

export default function RunDetail() {
  const { id } = useParams();
  const qc = useQueryClient();
  const [live, setLive] = useState<RunLog[]>([]);
  const [streaming, setStreaming] = useState(false);
  const termRef = useRef<HTMLDivElement>(null);

  const { data, error, isLoading } = useQuery({
    queryKey: ["run", id],
    queryFn: () => api<RunDetailDTO>(`/api/runs/${id}`),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 4000 : false),
  });

  // SSE 实时日志：连接期间直接看 agent 干活
  useEffect(() => {
    if (!id || !data || data.status !== "running") return;
    const es = new EventSource(`/api/runs/${id}/stream`);
    es.onopen = () => setStreaming(true);
    es.onmessage = (e) => {
      try {
        setLive((prev) => [...prev, JSON.parse(e.data)]);
      } catch {
        /* 忽略坏帧 */
      }
    };
    es.addEventListener("done", () => {
      setStreaming(false);
      es.close();
      qc.invalidateQueries({ queryKey: ["run", id] });
      qc.invalidateQueries({ queryKey: ["runs"] });
    });
    es.onerror = () => {
      setStreaming(false);
      es.close();
    };
    return () => {
      es.close();
      setStreaming(false);
    };
  }, [id, data?.status, qc]);

  useEffect(() => {
    termRef.current?.scrollTo({ top: termRef.current.scrollHeight });
  }, [live.length]);

  if (isLoading) return <Loading />;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  const logs = streaming && live.length ? live : data.logs;

  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">RUN #{data.id}</h1>
        <span className="page-sub">
          {data.repo} · {data.trigger === "manual" ? "手动" : "定时"}
          {streaming && " · ● 实时"}
        </span>
      </div>

      <Card title="PARAMETERS / 参数">
        <table className="tbl">
          <tbody>
            <tr>
              <td className="dim small" style={{ width: 120 }}>状态</td>
              <td><Badge status={data.status} /></td>
            </tr>
            <tr>
              <td className="dim small">模式</td>
              <td><ModeTag mode={data.mode} /></td>
            </tr>
            <tr>
              <td className="dim small">游标</td>
              <td className="mono small">
                <Sha sha={data.old_sha} /> → <Sha sha={data.new_sha} />
              </td>
            </tr>
            <tr>
              <td className="dim small">情报仓库提交</td>
              <td>
                <Sha sha={data.commit_sha} />{" "}
                {data.pushed ? "（已推送到情报仓库）" : "（尚未推送到 GitHub，下轮自动补推）"}
              </td>
            </tr>
            {data.pr_url && (
              <tr>
                <td className="dim small">Pull Request</td>
                <td>
                  <a href={data.pr_url} target="_blank" rel="noreferrer" className="mono">
                    #{data.pr_number} → 在 GitHub 审核
                  </a>
                </td>
              </tr>
            )}
            <tr>
              <td className="dim small">开始 / 结束</td>
              <td className="small dim">
                {fmtTime(data.started_at)} → {fmtTime(data.finished_at)}
              </td>
            </tr>
            {data.summary && (
              <tr>
                <td className="dim small">摘要</td>
                <td>{data.summary}</td>
              </tr>
            )}
            {data.error && (
              <tr>
                <td className="dim small">错误</td>
                <td>
                  <div className="err-box">{data.error}</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>

      <Card title="LOG / 运行日志">
        {logs.length === 0 ? (
          <div className="empty">无日志</div>
        ) : (
          <div className="term" ref={termRef}>
            {logs.map((l, i) => (
              <div key={i} className="ln">
                <span className="ts">{(l.ts || "").slice(11, 19)}</span>
                <span className={`lv-${l.level}`}>{l.message}</span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
