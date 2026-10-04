import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import Markdown from "../components/Markdown";
import { Card, Empty, ErrorBox, Loading, PageHelp } from "../components/ui";

export default function Daily() {
  const [params, setParams] = useSearchParams();
  const datesQ = useQuery({
    queryKey: ["daily"],
    queryFn: () => api<{ dates: string[] }>("/api/daily"),
  });
  const dates = datesQ.data?.dates ?? [];
  const selected = params.get("date") || dates[0] || "";

  const oneQ = useQuery({
    queryKey: ["daily", selected],
    queryFn: () => api<{ date: string; content: string }>(`/api/daily/${selected}`),
    enabled: !!selected,
  });

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head-left">
          <h1 className="page-title">每日晨报</h1>
          <PageHelp title="这一页是什么意思">
            <div>
              每天早上 8 点（可配）Gitwire 自动把前一天所有监控仓库的动态汇总成一份晨报：哪些仓库有新提交、机会池变化（新增 / 被占 / 回流）、值得注意的警报。左侧选期数，右侧看当期内容。
            </div>
            <div className="dim">晨报也同时存进情报仓库，可以在仓库的 daily/ 目录里找到历史版本。</div>
          </PageHelp>
        </div>
      </div>
      <div className="split" style={{ gridTemplateColumns: "220px 1fr" }}>
        <Card title={`ISSUES / 期数（${dates.length}）`}>
          {datesQ.isLoading ? (
            <Loading />
          ) : dates.length === 0 ? (
            <Empty text="还没有晨报" />
          ) : (
            <div className="file-list">
              {dates.map((d) => (
                <div
                  key={d}
                  className={`file-item${d === selected ? " active" : ""}`}
                  onClick={() => setParams({ date: d })}
                >
                  ◈ {d}
                </div>
              ))}
            </div>
          )}
        </Card>
        <Card title={`DIGEST / ${selected || "—"}`}>
          {oneQ.isLoading ? (
            <Loading />
          ) : oneQ.error ? (
            <ErrorBox error={oneQ.error} />
          ) : oneQ.data ? (
            <Markdown text={oneQ.data.content} />
          ) : (
            <Empty text="选择日期" />
          )}
        </Card>
      </div>
    </div>
  );
}
