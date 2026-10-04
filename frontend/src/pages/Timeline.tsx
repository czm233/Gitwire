import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { DiffFile, TimelineEntry } from "../types";
import DiffView from "../components/DiffView";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";

export default function Timeline() {
  const { slug } = useParams();
  const [params, setParams] = useSearchParams();
  const selectedSha = params.get("sha") || "";

  const tlQ = useQuery({
    queryKey: ["timeline", slug],
    queryFn: () => api<{ entries: TimelineEntry[] }>(`/api/repos/${slug}/timeline`),
  });

  const diffQ = useQuery({
    queryKey: ["version", slug, selectedSha],
    queryFn: () => api<{ files: DiffFile[] }>(`/api/repos/${slug}/timeline/${selectedSha}`),
    enabled: !!selectedSha,
  });

  if (tlQ.isLoading) return <Loading />;
  if (tlQ.error) return <ErrorBox error={tlQ.error} />;
  const entries = tlQ.data?.entries ?? [];
  if (entries.length === 0) {
    return <Empty text="该项目还没有同步版本" />;
  }

  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">变更时间线</h1>
        <span className="page-sub">
          {slug} ·{" "}
          <Link to={`/repos/${slug}`} className="small">
            返回档案
          </Link>
        </span>
      </div>

      <div className="split" style={{ gridTemplateColumns: "300px 1fr" }}>
        <Card title={`VERSIONS / 版本（${entries.length}）`}>
          <div className="file-list">
            {entries.map((e) => (
              <div
                key={e.sha}
                className={`file-item${e.sha === selectedSha ? " active" : ""}`}
                onClick={() => setParams({ sha: e.sha })}
              >
                <div>
                  <span className="sha">{e.sha.slice(0, 7)}</span>{" "}
                  <span className="dim">{e.date.slice(0, 10)} {e.date.slice(11, 16)}</span>
                </div>
                <div style={{ color: "var(--ink)" }}>{e.subject}</div>
                <div className="dim" style={{ fontSize: 11 }}>
                  {e.files.length} 个文件
                  {e.files.slice(0, 3).map((f) => ` · ${f.path.split("/").pop()}`).join("")}
                </div>
              </div>
            ))}
          </div>
        </Card>

        <div>
          {!selectedSha ? (
            <Card title="DIFF / 版本差异">
              <Empty text="← 选择一个版本查看该次同步的档案差异" />
            </Card>
          ) : diffQ.isLoading ? (
            <Loading />
          ) : diffQ.error ? (
            <ErrorBox error={diffQ.error} />
          ) : (
            <div className="stack">
              {(diffQ.data?.files ?? []).map((f) => (
                <Card key={f.path} title={f.path.replace(/^[^/]+\//, "")}>
                  <DiffView patch={f.patch} />
                </Card>
              ))}
              {(diffQ.data?.files ?? []).length === 0 && <Empty text="该版本未改动档案" />}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
