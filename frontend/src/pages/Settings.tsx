import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { Card, ErrorBox, Loading, PageHelp } from "../components/ui";

/** 设置页：一组设置一个 Card，后续新设置往这页加 */
export default function Settings() {
  const qc = useQueryClient();
  const { data, error, isLoading } = useQuery({
    queryKey: ["repos"],
    queryFn: () =>
      api<{ publish_mode: string; unwatch_closed: boolean; vault_remote: boolean }>(
        "/api/repos",
      ),
  });

  const modeMut = useMutation({
    mutationFn: (m: string) =>
      post<{ publish_mode: string }>("/api/repos/config/publish-mode", {
        publish_mode: m,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["repos"] });
    },
  });

  const unwatchMut = useMutation({
    mutationFn: (v: boolean) =>
      post<{ unwatch_closed: boolean; pruned: string[] }>(
        "/api/repos/config/unwatch-closed",
        { unwatch_closed: v },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["issues"] });
    },
  });

  return (
    <div className="stack">
      <div className="page-head">
        <div className="page-head-left">
          <h1 className="page-title">设置</h1>
          <PageHelp title="这一页是什么意思">
            <div>
              这里放影响 Gitwire 行为方式的开关；每一项的含义在各自卡片里说明了。
            </div>
            <div>
              <p className="modal-title" style={{ margin: "0 0 8px" }}>报告保存方式</p>
              <div style={{ display: "grid", gap: 6 }}>
                <div><b>直接保存</b>：每轮分析完自动提交并推送进 GitHub 情报仓库，全程无需操作；</div>
                <div><b>先开 PR</b>：每次先在情报仓库开一个 PR，你人工审核合入后才算发布——更稳，适合先观察一段时间产出的质量。</div>
              </div>
            </div>
            <div className="dim">其余配置（LLM 渠道、GitHub Token、Bark 推送地址、扫描频率）在项目根目录的 .env 文件里，改完需重启后端生效。</div>
          </PageHelp>
        </div>
      </div>

      {isLoading ? (
        <Loading />
      ) : error ? (
        <ErrorBox error={error} />
      ) : (
        <>
        <Card title="PUBLISH / 报告保存方式">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className={`btn${data?.publish_mode === "direct" ? "" : " ghost"}`}
                disabled={modeMut.isPending || data?.publish_mode === "direct"}
                onClick={() => modeMut.mutate("direct")}
              >
                {data?.publish_mode === "direct" ? "✓ 直接保存（当前）" : "改为直接保存"}
              </button>
              <span className="dim small">每轮分析完，报告自动存进 GitHub 情报仓库，不需要你操作</span>
            </div>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className={`btn${data?.publish_mode === "pr" ? "" : " ghost"}`}
                disabled={modeMut.isPending || data?.publish_mode === "pr" || data?.vault_remote === false}
                title={data?.vault_remote === false ? "需要配置 GitHub 远端情报仓库" : undefined}
                onClick={() => modeMut.mutate("pr")}
              >
                {data?.publish_mode === "pr" ? "✓ 先审核再保存（当前）" : "改为先审核再保存"}
              </button>
              <span className="dim small">每份报告先在 GitHub 开一个 PR，你点合并后才入库</span>
            </div>
          </div>
        </Card>
        <Card title="ISSUE 追踪 / 已关闭的 issue">
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className={`btn${!data?.unwatch_closed ? "" : " ghost"}`}
                disabled={unwatchMut.isPending || data?.unwatch_closed === false}
                onClick={() => unwatchMut.mutate(false)}
              >
                {!data?.unwatch_closed ? "✓ 继续追踪（当前）" : "改为继续追踪"}
              </button>
              <span className="dim small">追踪的 issue 关闭后仍保持追踪，直到你手动取消</span>
            </div>
            <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <button
                className={`btn${data?.unwatch_closed ? "" : " ghost"}`}
                disabled={unwatchMut.isPending || data?.unwatch_closed === true}
                onClick={() => unwatchMut.mutate(true)}
              >
                {data?.unwatch_closed ? "✓ 已关闭自动撤销追踪（当前）" : "改为已关闭自动撤销追踪"}
              </button>
              <span className="dim small">追踪的 issue 一旦关闭（做完了）就自动移出追踪清单；开启时会立即清掉现已关闭的追踪</span>
            </div>
            {unwatchMut.data?.pruned?.length ? (
              <div className="dim small">已清理：{unwatchMut.data.pruned.join("；")}</div>
            ) : null}
          </div>
        </Card>
        </>
      )}
    </div>
  );
}
