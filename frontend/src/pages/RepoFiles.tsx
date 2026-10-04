import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { FileEntry } from "../types";
import Markdown from "../components/Markdown";
import { Card, Empty, ErrorBox, Loading } from "../components/ui";

function fileIcon(path: string): string {
  if (path.startsWith("changelog/")) return "◈";
  if (path === "meta.yml") return "◉";
  if (path.endsWith(".yml")) return "≡";
  return "·";
}

export default function RepoFiles() {
  const { slug } = useParams();
  const [params, setParams] = useSearchParams();
  const selected = params.get("file") || "README.md";

  const filesQ = useQuery({
    queryKey: ["files", slug],
    queryFn: () => api<{ files: FileEntry[] }>(`/api/repos/${slug}/files`),
  });
  const fileQ = useQuery({
    queryKey: ["file", slug, selected],
    queryFn: () =>
      api<{ path: string; content: string }>(
        `/api/repos/${slug}/file?path=${encodeURIComponent(selected)}`,
      ),
    enabled: !!slug,
  });

  if (filesQ.isLoading) return <Loading />;
  if (filesQ.error) return <ErrorBox error={filesQ.error} />;
  const files = filesQ.data?.files ?? [];
  if (files.length === 0) return <Empty text="该项目还没有档案" />;

  return (
    <div className="stack">
      <div className="page-head">
        <h1 className="page-title">{slug}</h1>
        <span className="row-actions">
          <Link className="btn ghost" to={`/repos/${slug}/timeline`}>
            变更时间线
          </Link>
        </span>
      </div>
      <div className="split">
        <Card title={`DOSSIER / 档案（${files.length}）`}>
          <div className="file-list">
            {files.map((f) => (
              <div
                key={f.path}
                className={`file-item${f.path === selected ? " active" : ""}`}
                onClick={() => setParams({ file: f.path })}
              >
                {fileIcon(f.path)} {f.path}
              </div>
            ))}
          </div>
        </Card>
        <Card title={`FILE / ${selected}`}>
          {fileQ.isLoading ? (
            <Loading />
          ) : fileQ.error ? (
            <ErrorBox error={fileQ.error} />
          ) : fileQ.data ? (
            fileQ.data.path.endsWith(".yml") ? (
              <pre className="mono small" style={{ whiteSpace: "pre-wrap" }}>
                {fileQ.data.content}
              </pre>
            ) : (
              <Markdown text={fileQ.data.content} />
            )
          ) : null}
        </Card>
      </div>
    </div>
  );
}
