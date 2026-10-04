import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { IssueListResp } from "../types";
import { Card, Empty, ErrorBox, FilterBar, FillerRows, ListPager, Loading, PageHelp, usePageSize } from "../components/ui";
import { GSelect } from "../components/GSelect";

const GROUP_TABS: { id: string; label: string; desc: string }[] = [
  { id: "all", label: "全部", desc: "" },
  { id: "opportunity", label: "可上手", desc: "issue 还开放 + 没人做 + AI 评估难度为简单或中等 → 适合动手" },
  { id: "hard", label: "不适合上手", desc: "issue 还开放，但 AI 评估为「困难」，或给出了不建议做的理由（理由见每行的灰色小标签）" },
  { id: "taken", label: "有人做了", desc: "已有人开了 PR、被官方指派，或在评论里认领" },
  { id: "closed", label: "已关闭", desc: "官方已关闭的 issue" },
  { id: "watched", label: "追踪中", desc: "你点了「追踪」的 issue，一有动静（被认领、被关闭等）就推警报给你；再点一次「追踪中」按钮取消" },
];
const GROUP_LABEL: Record<string, string> = {
  opportunity: "可上手",
  hard: "不适合上手",
  taken: "有人做了",
  closed: "已关闭",
};
const GROUP_CLASS: Record<string, string> = {
  opportunity: "run",
  hard: "mut",
  taken: "warn",
  closed: "mut",
};
const DIFFICULTY_CLASS: Record<string, string> = {
  简单: "ok",
  中等: "run",
  困难: "warn",
};

// 口认领黏性天数（与后端 app/engine/recipes.py 的 CLAIM_TTL_DAYS 一致）
const CLAIM_TTL_DAYS = 14;

function daysSince(iso: string): number {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? 0 : Math.max(0, Math.floor((Date.now() - t) / 86400000));
}

function takenBadgeSuffix(it: IssueListResp["items"][number]): string {
  if (it.group !== "taken") return "";
  if (it.status === "taken-assignee") return it.assignee ? ` · ${it.assignee}` : "";
  if (it.status === "taken-pr") {
    const n = it.pr_claims[0] ?? it.pr_evidence[0]?.number;
    return n ? ` · PR#${n}` : "";
  }
  return it.claimed_by ? ` · ${it.claimed_by}` : "";
}

function TakenEvidence({ it }: { it: IssueListResp["items"][number] }) {
  if (it.status === "taken-assignee") {
    return (
      <div>
        <span className="dim">被占依据：</span>
        官方指派给 <b>{it.assignee || "未知用户"}</b>（GitHub 的 assignee 字段，{" "}
        <a href={it.url} target="_blank" rel="noreferrer">issue 页</a>可见；取消指派会自动放回机会池）
      </div>
    );
  }
  if (it.status === "taken-pr") {
    return (
      <div>
        <span className="dim">被占依据：</span>
        <div style={{ display: "grid", gap: 4, marginTop: 4 }}>
          {it.pr_evidence.length ? (
            <>
              {it.pr_evidence.map((p) => (
                <div key={p.number}>
                  <a href={p.url} target="_blank" rel="noreferrer" className="mono">PR #{p.number}</a>{" "}
                  『{p.title || "无标题"}』— {p.author || "?"}
                  {p.created_at ? ` · 已开 ${daysSince(p.created_at)} 天未合入` : ""}
                </div>
              ))}
              <div className="dim">
                判定规则：打开状态的 PR，标题或正文含 fixes / closes / resolves #{it.number}；PR 被关闭不合入会自动放回机会池
              </div>
            </>
          ) : (
            <div>
              PR {it.pr_claims.length ? it.pr_claims.map((n) => `#${n}`).join("、") : "?"} 声称修复
              （PR 详情下轮扫描补齐）
            </div>
          )}
        </div>
      </div>
    );
  }
  if (it.status === "taken-claim" && it.claimed_by) {
    return (
      <div>
        <span className="dim">被占依据：</span>
        <b>{it.claimed_by}</b> 在评论中认领
        {it.claim_url ? (
          <>
            （<a href={it.claim_url} target="_blank" rel="noreferrer">看认领评论</a>）
          </>
        ) : null}
        {it.claimed_at ? ` · 认领于 ${it.claimed_at.slice(0, 10)}（已 ${daysSince(it.claimed_at)} 天）` : ""}
        {` · ${CLAIM_TTL_DAYS} 天内没开 PR 会自动放回机会池`}
      </div>
    );
  }
  return null;
}

export default function Issues() {
  const qc = useQueryClient();
  const [group, setGroup] = useState("all"); // 默认全量视角（用户要求：打开就要看到项目所有 issue）
  const [repo, setRepo] = useState("");
  const [difficulty, setDifficulty] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = usePageSize("issues");
  const [expanded, setExpanded] = useState<string | null>(null);

  const params = new URLSearchParams({
    group,
    page: String(page),
    page_size: String(pageSize),
  });
  if (repo) params.set("repo", repo);
  if (difficulty) params.set("difficulty", difficulty);
  if (q.trim()) params.set("q", q.trim());

  const { data, error, isLoading } = useQuery({
    queryKey: ["issues", group, repo, difficulty, q.trim(), page, pageSize],
    queryFn: () => api<IssueListResp>(`/api/issues?${params.toString()}`),
    refetchInterval: 30000,
  });

  const watchMut = useMutation({
    mutationFn: (v: { repo: string; number: number }) => post("/api/issues/watch", v),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["issues"] });
    },
  });

  const setTab = (g: string) => {
    setGroup(g);
    setPage(1);
    setExpanded(null);
  };

  const stats = data?.stats;

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head-left">
          <h1 className="page-title">Issue 雷达</h1>
          <PageHelp title="这一页是什么意思">
            <div>
              Gitwire 会自动用 AI 逐个分析你监控的开源项目的开放 issue：问题是什么、怎么解、难度多大、有没有人已经在做。这一页把所有项目的分析结果放在一起，帮你挑值得动手的去贡献。
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>四个分组怎么分的</p>
              <div style={{ display: "grid", gap: 8 }}>
                <div>
                  <span className="badge run">可上手</span>{" "}
                  issue 还开放、没人做，AI 评估难度为简单或中等 → 适合动手
                </div>
                <div>
                  <span className="badge mut">不适合上手</span>{" "}
                  issue 还开放，但 AI 评估为「困难」，或给出了不建议做的理由（理由显示在每行的灰色小标签上）
                </div>
                <div>
                  <span className="badge warn">有人做了</span>{" "}
                  已有人开了 PR、被官方指派，或在评论里认领；展开行能看到被谁占、凭什么判（PR 链接 / 指派人 / 认领评论）
                </div>
                <div>
                  <span className="badge mut">已关闭</span> 官方已关闭的 issue
                </div>
              </div>
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>「不适合上手」是怎么判的</p>
              <div style={{ display: "grid", gap: 8 }}>
                <div>开放且没人做的 issue，命中下面任意一条就归入这组：</div>
                <div>① AI 读完 issue 后评估难度为「困难」；</div>
                <div>
                  ② 命中一条「不建议做」的规则——每行 issue 下方的灰色小标签就是它具体中的是哪条：
                  <div style={{ paddingLeft: 14, marginTop: 4, display: "grid", gap: 4 }}>
                    <div>· 仓库 30 天没有提交 → 项目可能不活跃</div>
                    <div>· 仓库 90 天没合并过外部 PR（或从未合并过）→ 做了也大概率合不进去</div>
                    <div>· issue 建了超过 30 天且近两周没动静 → 多半凉了</div>
                    <div>· 评论超过 15 条 → 竞争过热，下手晚了</div>
                  </div>
                </div>
                <div className="dim">以上天数 / 条数是默认阈值，可在配置里按仓库调整。</div>
              </div>
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>难度和结论是哪来的</p>
              AI 读了 issue 的标题、描述和评论后给出的判断，仅供参考。点每行最左边的 ▸ 可以展开看 AI 写的「问题 / 方案」详情。
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>「追踪」是什么</p>
              点了「追踪」的 issue 一有动静（被认领、被关闭等）就推警报给你。再点一次取消追踪。点统计条最右的「追踪中」卡片，列表就只显示这些。
              在「设置」页开启「已关闭自动撤销追踪」后，追踪的 issue 一旦关闭（做完了）会自动移出追踪清单，不用手动取消。
            </div>
          </PageHelp>
        </div>
      </div>

      <div className="stats-row">
        <div
          className={`stat card stat-tab${group === "all" ? " active" : ""}`}
          onClick={() => setTab("all")}
          title="不分难度和状态：这个项目所有被收录的 issue"
        >
          <span className="tick-b" />
          <div className="num">
            {stats ? stats.opportunity + stats.hard + stats.taken + stats.closed : "…"}
          </div>
          <div className="label">全部</div>
        </div>
        {GROUP_TABS.filter((t) => t.id !== "all").map((t) => (
          <div
            key={t.id}
            className={`stat card stat-tab${group === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
            title={t.desc}
          >
            <span className="tick-b" />
            <div className={`num ${t.id === "opportunity" || t.id === "watched" ? "" : "amber"}`}>
              {stats ? stats[t.id as keyof typeof stats] : "…"}
            </div>
            <div className="label">{t.label}</div>
          </div>
        ))}
      </div>

      <Card>
        <FilterBar>
          <GSelect
            value={group}
            onChange={setTab}
            options={GROUP_TABS.map((t) => ({ value: t.id, label: t.label }))}
          />
          <GSelect
            value={repo}
            onChange={(v) => {
              setRepo(v);
              setPage(1);
            }}
            placeholder="全部仓库"
            options={(data?.repos ?? []).map((r) => ({ value: r, label: r }))}
          />
          <GSelect
            value={difficulty}
            onChange={(v) => {
              setDifficulty(v);
              setPage(1);
            }}
            placeholder="全部难度"
            options={[
              { value: "简单", label: "简单" },
              { value: "中等", label: "中等" },
              { value: "困难", label: "困难" },
            ]}
          />
          <input
            className="input"
            style={{ minWidth: 180 }}
            placeholder="搜标题 / 一句话结论…"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setPage(1);
            }}
          />
        </FilterBar>
        {isLoading ? (
          <Loading />
        ) : error ? (
          <ErrorBox error={error} />
        ) : !data || data.items.length === 0 ? (
          <Empty
            text={
              group === "watched"
                ? "还没有追踪中的 issue——在别的分组里看中哪个点「追踪」，它就会出现在这里"
                : "没有符合筛选的 issue（前提：在「监控清单」→ 项目「设置」里勾选「issue 雷达」）"
            }
          />
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th style={{ width: 30 }}></th>
                <th>Issue</th>
                <th>项目</th>
                <th>难度</th>
                <th>状态</th>
                <th style={{ width: 76 }}>追踪</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <ItemRow
                  key={`${it.repo}#${it.number}`}
                  it={it}
                  expanded={expanded === `${it.repo}#${it.number}`}
                  onExpand={() =>
                    setExpanded(expanded === `${it.repo}#${it.number}` ? null : `${it.repo}#${it.number}`)
                  }
                  onWatch={() => watchMut.mutate({ repo: it.repo, number: it.number })}
                  watchPending={watchMut.isPending && watchMut.variables?.number === it.number}
                />
              ))}
              <FillerRows cols={6} pageSize={data.pages > 1 ? pageSize : 0} />
            </tbody>
          </table>
        )}
        {data && (
          <ListPager
            page={data.page}
            pages={data.pages}
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

function ItemRow({
  it,
  expanded,
  onExpand,
  onWatch,
  watchPending,
}: {
  it: IssueListResp["items"][number];
  expanded: boolean;
  onExpand: () => void;
  onWatch: () => void;
  watchPending: boolean;
}) {
  return (
    <>
      <tr className="reveal">
        <td>
          <button className="btn" style={{ padding: "0 8px" }} onClick={onExpand} title="展开分析详情">
            {expanded ? "▾" : "▸"}
          </button>
        </td>
        <td>
          <a href={it.url} target="_blank" rel="noreferrer" className="mono">
            #{it.number}
          </a>{" "}
          <a href={it.url} target="_blank" rel="noreferrer">
            {it.title}
          </a>
          <div className="dim small" style={{ marginTop: 2 }}>
            {it.excluded ? (
              <span
                className="badge mut"
                style={{ marginRight: 6 }}
                title="不建议做的具体原因（规则判定）。完整判定规则点页面右上角 ?"
              >
                {it.excluded}
              </span>
            ) : null}
            {it.summary || "—"}
          </div>
        </td>
        <td className="small">
          <Link to={`/repos/${it.slug}`} title="看项目档案（架构/技术栈）">
            {it.repo}
          </Link>
        </td>
        <td>
          <span
            className={`badge ${DIFFICULTY_CLASS[it.difficulty] || "mut"}`}
            title="AI 读完 issue 内容后评估的难度，仅供参考；判定逻辑点页面右上角 ?"
          >
            {it.difficulty}
          </span>
        </td>
        <td>
          <span className={`badge ${GROUP_CLASS[it.group] || "mut"}`}>
            {(GROUP_LABEL[it.group] || it.status) + takenBadgeSuffix(it)}
          </span>
        </td>
        <td>
          <button
            className={`btn ${it.watched ? "" : "ghost"}`}
            style={{ padding: "2px 8px" }}
            disabled={watchPending}
            onClick={onWatch}
            title={it.watched ? "取消追踪" : "追踪：任何动态推 watch 警报"}
          >
            {it.watched ? "追踪中" : "追踪"}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="issue-detail-row">
          <td></td>
          <td colSpan={5}>
            <div className="small" style={{ display: "grid", gap: 6, padding: "4px 0" }}>
              {it.group === "taken" ? <TakenEvidence it={it} /> : null}
              <div>
                <span className="dim">问题：</span>
                {it.problem || "—"}
              </div>
              <div>
                <span className="dim">方案：</span>
                {it.plan || "—"}
              </div>
              <div className="dim">
                {it.labels.length ? `标签 ${it.labels.join(", ")} · ` : ""}评论 {it.comments} 条
                {it.created_at ? ` · 建于 ${it.created_at.slice(0, 10)}` : ""}
                {it.resolved_by ? ` · ${it.resolved_by}` : ""}
                {it.analyzed_at ? ` · AI 分析于 ${it.analyzed_at}` : ""}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
