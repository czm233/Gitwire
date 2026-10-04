import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { RunListResp } from "../types";
import {
  Badge,
  Card,
  Empty,
  ErrorBox,
  FilterBar,
  FillerRows,
  ListPager,
  Loading,
  ModeTag,
  PageHelp,
  TimeCell,
  usePageSize,
} from "../components/ui";
import { GSelect } from "../components/GSelect";

export default function RunsList() {
  const [repo, setRepo] = useState("");
  const [status, setStatus] = useState("");
  const [trigger, setTrigger] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePageSize("runs");

  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (repo) params.set("repo", repo);
  if (status) params.set("status", status);
  if (trigger) params.set("trigger", trigger);

  const { data, error, isLoading } = useQuery({
    queryKey: ["runs", repo, status, trigger, page, pageSize],
    queryFn: () => api<RunListResp>(`/api/runs?${params.toString()}`),
    refetchInterval: (q) =>
      q.state.data?.runs.some((r) => r.status === "running") ? 3000 : 20000,
  });

  const reposQuery = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<{ repos: { repo: string }[] }>("/api/repos"),
  });

  const pick = (set: (v: string) => void) => (v: string) => {
    set(v);
    setPage(1);
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head-left">
          <h1 className="page-title">运行历史</h1>
          <PageHelp title="这一页是什么意思">
            <div>
              Gitwire 每次干活（分析一个仓库并把结果存进情报仓库）在这里留一条记录，一行一次运行。点任意一行进详情，能看完整日志和这轮产出的文件。
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>每列是什么意思</p>
              <div style={{ display: "grid", gap: 6 }}>
                <div><b>模式</b>：建档 = 第一次分析这个仓库，全量深读建档案；增量 = 仓库有新提交，只分析变化的部分</div>
                <div><b>状态</b>：排队中 → 进行中 → 已发布（结果已存进情报仓库）或 失败（红色，摘要列显示原因）</div>
                <div><b>触发</b>：定时 = 每小时自动扫描带出来的；手动 = 有人在监控清单点了「立即同步」</div>
                <div><b>摘要</b>：这轮的结论。雷达类运行的摘要如「机会 0（新 0）· 未入榜 29 · 被占 10」——机会 = 可上手的 issue，未入榜 = 开放但不建议做，被占 = 已经有人在做了</div>
              </div>
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>情报仓库是什么</p>
              项目档案、晨报这些产出发布到的独立 git 仓库（见监控清单里各项目的档案链接），本页的「已发布」就是指结果提交进了那个仓库。
            </div>
          </PageHelp>
        </div>
      </div>
      <Card>
        <FilterBar>
          <GSelect
            value={repo}
            onChange={pick(setRepo)}
            placeholder="全部仓库"
            options={(reposQuery.data?.repos ?? []).map((r) => ({ value: r.repo, label: r.repo }))}
          />
          <GSelect
            value={status}
            onChange={pick(setStatus)}
            placeholder="全部状态"
            options={[
              { value: "running", label: "运行中" },
              { value: "published", label: "已发布" },
              { value: "failed", label: "失败" },
            ]}
          />
          <GSelect
            value={trigger}
            onChange={pick(setTrigger)}
            placeholder="全部触发"
            options={[
              { value: "scheduled", label: "定时" },
              { value: "manual", label: "手动" },
            ]}
          />
        </FilterBar>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <ErrorBox error={error} />
        ) : !data || data.runs.length === 0 ? (
          <Empty text="没有符合筛选的运行记录" />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>#</th>
                <th>仓库</th>
                <th>模式</th>
                <th>状态</th>
                <th>触发</th>
                <th>开始</th>
                <th>结束</th>
                <th>摘要/错误</th>
              </tr>
            </thead>
            <tbody>
              {data.runs.map((r) => (
                <tr key={r.id}>
                  <td className="mono dim">{r.id}</td>
                  <td>
                    <Link to={`/runs/${r.id}`} className="mono">
                      {r.repo}
                    </Link>
                  </td>
                  <td>
                    <ModeTag mode={r.mode} />
                    {r.pr_url && (
                      <>
                        {" "}
                        <a href={r.pr_url} target="_blank" rel="noreferrer" className="badge warn">
                          PR
                        </a>
                      </>
                    )}
                  </td>
                  <td>
                    <Badge status={r.status} />
                  </td>
                  <td className="dim small nw">{r.trigger === "manual" ? "手动" : "定时"}</td>
                  <td className="dim small"><TimeCell iso={r.started_at} /></td>
                  <td className="dim small"><TimeCell iso={r.finished_at} /></td>
                  <td className="small">
                    {r.status === "failed" ? (
                      <span style={{ color: "var(--red)" }}>{r.error?.slice(0, 120)}</span>
                    ) : (
                      r.summary || <span className="dim">—</span>
                    )}
                  </td>
                </tr>
              ))}
              <FillerRows
                cols={8}
                pageSize={Math.ceil(data.total / pageSize) > 1 ? pageSize : 0}
              />
            </tbody>
          </table>
        )}
        {data && (
          <ListPager
            page={data.page}
            pages={Math.max(1, Math.ceil(data.total / pageSize))}
            total={data.total}
            pageSize={pageSize}
            onPage={setPage}
            onPageSize={(n) => {
              setPageSize(n);
              setPage(1);
            }}
          />
        )}
      </Card>
    </div>
  );
}
