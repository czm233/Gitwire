import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, del, patch, post } from "../api";
import { RepoRow } from "../types";
import { Card, Confirm, Empty, ErrorBox, FilterBar, FillerRows, ListPager, Loading, Modal, PageHelp, TimeCell, usePageSize } from "../components/ui";
import { GSelect } from "../components/GSelect";

const RECIPE_OPTIONS: { id: string; label: string }[] = [
  { id: "docs-sync", label: "档案同步" },
  { id: "feature-tripwire", label: "哨兵警戒" },
  { id: "bug-watch", label: "缺陷观察" },
  { id: "cve-scan", label: "依赖漏洞" },
  { id: "issue-radar", label: "issue 雷达" },
];
const RECIPE_LABEL: Record<string, string> = Object.fromEntries(
  RECIPE_OPTIONS.map(({ id, label }) => [id, label])
);

/** 项目设置弹窗：配方勾选保存；追踪只展示标签（单个 × 取消），添加入口在 Issue 专区「追踪」 */
function RepoSettings({ row, onClose }: { row: RepoRow; onClose: () => void }) {
  const qc = useQueryClient();
  const [recipes, setRecipes] = useState<string[]>(row.recipes);
  const [watched, setWatched] = useState<number[]>(row.watch_issues);
  const [err, setErr] = useState("");

  const saveMut = useMutation({
    mutationFn: (payload: { recipes: string[] }) => patch(`/api/repos/${row.slug}`, payload),
    onSuccess: () => {
      onClose();
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["issues"] });
    },
    onError: (e) => setErr(String(e instanceof Error ? e.message : e)),
  });

  const unwatchMut = useMutation({
    mutationFn: (numbers: number[]) =>
      patch(`/api/repos/${row.slug}`, { watch_issues: numbers }),
    onSuccess: (_d, numbers) => {
      setWatched(numbers);
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["issues"] });
    },
    onError: (e) => setErr(String(e instanceof Error ? e.message : e)),
  });

  const toggle = (id: string) =>
    setRecipes((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const slash = row.repo.indexOf("/");
  const owner = slash > 0 ? row.repo.slice(0, slash) : row.repo;
  const name = slash > 0 ? row.repo.slice(slash + 1) : row.repo;

  return (
    <Modal
      title="项目设置"
      subtitle={
        <>
          {owner} / <span className="sub-name">{name}</span>
        </>
      }
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button
            className="btn"
            disabled={saveMut.isPending || recipes.length === 0}
            onClick={() => saveMut.mutate({ recipes })}
          >
            {saveMut.isPending ? "保存中…" : "保存"}
          </button>
        </>
      }
    >
      <div>
        <p className="modal-title" style={{ marginBottom: 8 }}>
          配方挂载（勾选即启用）
        </p>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {RECIPE_OPTIONS.map(({ id, label }) => (
            <label key={id} className={`tag ${recipes.includes(id) ? "on" : ""}`} style={{ cursor: "pointer", padding: "4px 10px" }}>
              <input
                type="checkbox"
                checked={recipes.includes(id)}
                onChange={() => toggle(id)}
                style={{ marginRight: 6 }}
              />
              {label}
            </label>
          ))}
        </div>
        <p className="dim small" style={{ margin: "8px 0 0" }}>
          issue 雷达 = issue 监控（机会榜/被占/口认领）；保存后新挂雷达会立即做一次全量盘点。
        </p>
      </div>
      <div>
        <p className="modal-title" style={{ marginBottom: 8 }}>
          issue 追踪
        </p>
        {watched.length ? (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {watched.map((n) => (
              <span key={n} className="tag on" style={{ padding: "3px 8px" }}>
                <a href={`https://github.com/${row.repo}/issues/${n}`} target="_blank" rel="noreferrer">
                  #{n}
                </a>
                <button
                  className="btn"
                  style={{ padding: "0 5px", marginLeft: 6, fontSize: 11 }}
                  title="取消追踪"
                  disabled={unwatchMut.isPending}
                  onClick={() => unwatchMut.mutate(watched.filter((x) => x !== n))}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : (
          <div className="dim small" style={{ margin: 0, display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            <span>还没有追踪的 issue</span>
            <Link className="btn ghost" to="/issues">
              去 Issue 雷达挑一个 →
            </Link>
          </div>
        )}
      </div>
      {err && <ErrorBox error={err} />}
    </Modal>
  );
}

/** 添加监控弹窗：仓库标识 + 起步配方；成功即关窗（后台排队首建档案） */
function AddRepoModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [recipes, setRecipes] = useState<string[]>(["docs-sync", "issue-radar"]);
  const [err, setErr] = useState("");

  const addMut = useMutation({
    mutationFn: (payload: { repo: string; recipes: string[] }) => post("/api/repos", payload),
    onSuccess: () => {
      onClose();
      qc.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: (e) => setErr(String(e instanceof Error ? e.message : e)),
  });

  const toggle = (id: string) =>
    setRecipes((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));

  const submit = () => {
    if (!input.trim()) return;
    addMut.mutate({ repo: input.trim(), recipes });
  };

  return (
    <Modal
      title="添加监控"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            取消
          </button>
          <button className="btn" disabled={!input.trim() || addMut.isPending} onClick={submit}>
            {addMut.isPending ? "验证中…" : "添加并建档"}
          </button>
        </>
      }
    >
      <div style={{ display: "grid", gap: 10 }}>
        <input
          className="input"
          placeholder="owner/name，或直接粘贴 GitHub 仓库 URL"
          value={input}
          autoFocus
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        <div>
          <p className="dim small" style={{ margin: "0 0 6px" }}>起步配方（之后可在项目设置里改）</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {RECIPE_OPTIONS.map(({ id, label }) => (
              <label key={id} className={`tag ${recipes.includes(id) ? "on" : ""}`} style={{ cursor: "pointer", padding: "4px 10px" }}>
                <input
                  type="checkbox"
                  checked={recipes.includes(id)}
                  onChange={() => toggle(id)}
                  style={{ marginRight: 6 }}
                />
                {label}
              </label>
            ))}
          </div>
        </div>
        <p className="dim small" style={{ margin: 0 }}>
          添加后立即排队首次建档；挂了「issue 雷达」才会出现在 Issue 雷达里。
        </p>
        {err && <ErrorBox error={err} />}
      </div>
    </Modal>
  );
}

export default function ReposManage() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [recipeFilter, setRecipeFilter] = useState("");
  const [pubFilter, setPubFilter] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePageSize("repos");
  const [setting, setSetting] = useState<RepoRow | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<RepoRow | null>(null);

  const { data, error, isLoading } = useQuery({
    queryKey: ["repos"],
    queryFn: () => api<{ repos: RepoRow[] }>("/api/repos"),
    // 有仓库在建档时 3 秒一轮盯着状态翻转（排队中→建档中→已建档），平时 20 秒常规刷新
    refetchInterval: (q) =>
      q.state.data?.repos.some((r) => r.last_status === "running") ? 3000 : 20000,
  });

  const delMut = useMutation({
    mutationFn: (slug: string) => del(`/api/repos/${slug}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["repos"] });
    },
  });

  const repos = data?.repos ?? [];
  const filtered = useMemo(
    () =>
      repos.filter(
        (r) =>
          (!search || r.repo.toLowerCase().includes(search.toLowerCase())) &&
          (!recipeFilter || r.recipes.includes(recipeFilter)) &&
          (!pubFilter || (pubFilter === "published") === r.published)
      ),
    [repos, search, recipeFilter, pubFilter]
  );
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const cur = Math.min(page, pages);
  const rows = filtered.slice((cur - 1) * pageSize, cur * pageSize);

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head-left">
          <h1 className="page-title">监控清单</h1>
          <PageHelp title="这一页是什么意思">
          <div>
            你让 Gitwire 盯着的 GitHub 仓库，一行一个项目。Gitwire 每小时自动扫一遍，有新提交或满足触发条件就按勾选的「配方」分析，结果发布到情报仓库（项目档案 / Issue 雷达 / 晨报的数据都来自这里）。
          </div>
          <div>
            <p className="modal-title" style={{ margin: "0 0 8px" }}>每列是什么意思</p>
            <div style={{ display: "grid", gap: 6 }}>
              <div><b>本轮</b>：下一次运行将采用的模式——建档 = 还没分析过，首轮全量深读；增量 = 已有档案，只跟新提交</div>
              <div><b>状态</b>：排队中 → 进行中 → 已发布（最近一轮结果已存进情报仓库）或 失败</div>
              <div><b>游标</b>：最近一次分析到的 commit（只跟 main 分支），有新提交游标才会前进、才会触发增量分析</div>
            </div>
          </div>
          <div>
            <p className="modal-title" style={{ margin: "0 0 8px" }}>配方是什么</p>
            <div style={{ display: "grid", gap: 6 }}>
              <div>每轮同步对这个仓库执行的分析动作，可多选：</div>
              <div><b>档案同步</b>：维护项目档案——技术栈、架构、业务逻辑、逐版本 changelog</div>
              <div><b>issue 雷达</b>：全量分析开放 issue（难度 / 方案 / 被占判定），Issue 雷达页的数据来源</div>
              <div><b>缺陷观察</b>：盯你指定的 issue，评论有动静就出警报</div>
              <div><b>哨兵警戒</b>：盯指定文件或特性，被外部改动就出警报</div>
              <div><b>依赖漏洞</b>：依赖出现 CVE 时警报</div>
              <div className="dim">新添加的仓库默认勾「档案同步 + issue 雷达」，添加弹窗里可改。</div>
            </div>
          </div>
        </PageHelp>
        </div>
        <button className="btn" onClick={() => setAdding(true)}>
          ＋ 添加监控
        </button>
      </div>

      <Card>
        <FilterBar>
          <input
            className="input"
            style={{ minWidth: 220 }}
            placeholder="按仓库名筛选…"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
          <GSelect
            value={recipeFilter}
            onChange={(v) => {
              setRecipeFilter(v);
              setPage(1);
            }}
            placeholder="全部配方"
            options={RECIPE_OPTIONS.map(({ id, label }) => ({ value: id, label: `挂了${label}` }))}
          />
          <GSelect
            value={pubFilter}
            onChange={(v) => {
              setPubFilter(v);
              setPage(1);
            }}
            placeholder="全部档案状态"
            options={[
              { value: "published", label: "已建档" },
              { value: "pending", label: "待建档" },
            ]}
          />
          <span className="filter-count">
            {filtered.length}/{repos.length}
          </span>
        </FilterBar>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <ErrorBox error={error} />
        ) : rows.length === 0 ? (
          <Empty text={repos.length ? "没有符合筛选的仓库" : "清单为空"} />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>作者 / 组织</th>
                <th>仓库</th>
                <th className="nw">建档状态</th>
                <th className="nw">建档时间</th>
                <th>配方</th>
                <th style={{ width: 150 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const slash = r.repo.indexOf("/");
                const owner = slash > 0 ? r.repo.slice(0, slash) : "";
                const name = slash > 0 ? r.repo.slice(slash + 1) : r.repo;
                return (
                  <tr key={r.slug}>
                    <td className="mono dim">{owner || "—"}</td>
                    <td className="mono">
                      <a href={`https://github.com/${r.repo}`} target="_blank" rel="noreferrer">
                        {name}
                      </a>
                    </td>
                    <td className="nw">
                      {r.last_status === "running" ? (
                        <span className="badge run">建档中</span>
                      ) : r.published ? (
                        <span className="badge ok">已建档</span>
                      ) : r.last_status === "failed" ? (
                        <span className="badge err">失败</span>
                      ) : (
                        <span className="badge mut">排队中</span>
                      )}
                    </td>
                    <td className="dim small"><TimeCell iso={r.archived_at} /></td>
                    <td>
                      {r.recipes.map((c) => (
                        <span key={c} className="tag">
                          {RECIPE_LABEL[c] || c}
                        </span>
                      ))}
                    </td>
                    <td>
                      <div style={{ display: "flex", gap: 6, flexWrap: "nowrap" }}>
                        <button className="btn" onClick={() => setSetting(r)}>
                          设置
                        </button>
                        <button
                          className="btn danger"
                          onClick={() => setRemoving(r)}
                        >
                          移除
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
              <FillerRows cols={6} pageSize={pages > 1 ? pageSize : 0} />
            </tbody>
          </table>
        )}
        <ListPager
          page={cur}
          pages={pages}
          total={filtered.length}
          pageSize={pageSize}
          onPage={setPage}
          onPageSize={(n) => {
            setPageSize(n);
            setPage(1);
          }}
        />
      </Card>
      {setting && <RepoSettings row={setting} onClose={() => setSetting(null)} />}
      {adding && <AddRepoModal onClose={() => setAdding(false)} />}
      <Confirm
        open={!!removing}
        title="移除监控"
        body={`确定移除 ${removing?.repo ?? ""} 吗？档案目录保留在 vault。`}
        confirmText="移除"
        danger
        onConfirm={() => {
          if (removing) delMut.mutate(removing.slug);
          setRemoving(null);
        }}
        onClose={() => setRemoving(null)}
      />
    </div>
  );
}
