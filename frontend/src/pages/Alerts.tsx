import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { AlertListResp } from "../types";
import {
  Card,
  Empty,
  ErrorBox,
  FilterBar,
  FillerRows,
  KindBadge,
  Loading,
  ListPager,
  PageHelp,
  TimeCell,
  usePageSize,
} from "../components/ui";
import { GSelect } from "../components/GSelect";

const KINDS = [
  "tripwire",
  "breaking",
  "release",
  "cve",
  "opportunity",
  "issue-taken",
  "issue-closed",
  "watch",
  "error",
];

export default function Alerts() {
  const [kind, setKind] = useState("");
  const [repo, setRepo] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePageSize("alerts");

  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  if (kind) params.set("kind", kind);
  if (repo) params.set("repo", repo);

  const { data, error, isLoading } = useQuery({
    queryKey: ["alerts", kind, repo, page, pageSize],
    queryFn: () => api<AlertListResp>(`/api/alerts?${params.toString()}`),
    refetchInterval: 20000,
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
          <h1 className="page-title">警报</h1>
          <PageHelp title="这一页是什么意思">
            <div>
              Gitwire 盯仓库时觉得值得你注意的事件流水：新机会出现、机会被人占了、同步失败、版本发布、依赖漏洞等，都在这里留档。默认最新在前。
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>类型有哪些</p>
              <div style={{ display: "grid", gap: 6 }}>
                <div><span className="badge run">参与机会</span> 新出现了值得上手的 issue（判定标准见 Issue 雷达页的 ?）</div>
                <div><span className="badge warn">机会被占</span> 上期还是机会的 issue 被人开了 PR / 指派 / 认领</div>
                <div><span className="badge mut">机会了结</span> 追踪的 issue 被官方关闭</div>
                <div><span className="badge warn">哨兵翻转</span> 你设的哨兵文件/特性被改动（监控清单里配）</div>
                <div><span className="badge ok">新版本</span> 仓库发了新 release，附 AI 简报</div>
                <div><span className="badge warn">漏洞</span> 依赖出现 CVE</div>
                <div><span className="badge ok">issue 追踪</span> 你点了「追踪」的 issue 有动静</div>
                <div><span className="badge err">运行失败</span> 某轮同步失败了，说明列里有原因</div>
              </div>
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>「推送」列：已推 / 仅台账</p>
              <div style={{ display: "grid", gap: 6 }}>
                <div><b>已推</b> = 这条警报同时通过 Bark 发到了 iPhone；</div>
                <div><b>仅台账</b> = 只记录在这一页，没有推手机。</div>
                <div className="dim">想收到手机推送：在 iPhone 装 Bark App，把它给的地址填进 .env 的 BARK_URL，重启后端即可；不填就全部只进台账。</div>
              </div>
            </div>
          </PageHelp>
        </div>
      </div>
      <Card>
        <FilterBar>
          <GSelect
            value={kind}
            onChange={pick(setKind)}
            placeholder="全部类型"
            options={KINDS.map((k) => ({ value: k, label: k }))}
          />
          <GSelect
            value={repo}
            onChange={pick(setRepo)}
            placeholder="全部仓库"
            options={(reposQuery.data?.repos ?? []).map((r) => ({ value: r.repo, label: r.repo }))}
          />
        </FilterBar>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <ErrorBox error={error} />
        ) : !data || data.alerts.length === 0 ? (
          <Empty text="没有符合筛选的警报——岁月静好" />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>时间</th>
                <th>类型</th>
                <th>仓库</th>
                <th>标题</th>
                <th>说明</th>
                <th>推送</th>
              </tr>
            </thead>
            <tbody>
              {data.alerts.map((a) => (
                <tr key={a.id} className="reveal">
                  <td className="dim small"><TimeCell iso={a.ts} /></td>
                  <td>
                    <KindBadge kind={a.kind} />
                  </td>
                  <td className="mono small">{a.repo || "—"}</td>
                  <td>{a.title}</td>
                  <td className="dim small">{a.body || "—"}</td>
                  <td className="dim small nw">{a.pushed ? "已推" : "仅台账"}</td>
                </tr>
              ))}
              <FillerRows
                cols={6}
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
