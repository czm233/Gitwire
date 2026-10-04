import { ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { GSelect } from "@/components/GSelect";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

/** 取景框卡片：四角刻度线是 Gitwire 的视觉签名；extra 渲染在标题行右侧 */
export function Card({
  title,
  extra,
  children,
  className = "",
}: {
  title?: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card reveal ${className}`}>
      <span className="tick-b" />
      {title || extra ? (
        <p
          className="card-title"
          style={extra ? { display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10 } : undefined}
        >
          {title ? <span>{title}</span> : null}
          {extra}
        </p>
      ) : null}
      {children}
    </section>
  );
}

export function Badge({ status }: { status: string }) {
  const map: Record<string, string> = {
    published: "ok",
    failed: "err",
    running: "run",
  };
  const label: Record<string, string> = {
    published: "已发布",
    failed: "失败",
    running: "运行中",
  };
  return <span className={`badge ${map[status] || "mut"}`}>{label[status] || status}</span>;
}

export function ModeTag({ mode }: { mode: string | null }) {
  const label: Record<string, string> = {
    init: "建档",
    incremental: "增量",
    noop: "无变化",
    "pr-pending": "PR 待审",
  };
  if (!mode) return <span className="dim">—</span>;
  const muted = mode === "noop";
  return <span className={`badge ${muted ? "mut" : "ok"}`}>{label[mode] || mode}</span>;
}

const KIND_LABEL: Record<string, string> = {
  tripwire: "哨兵翻转",
  breaking: "破坏性变更",
  release: "新版本",
  cve: "漏洞",
  error: "运行失败",
  opportunity: "参与机会",
  "issue-taken": "机会被占",
  "issue-closed": "机会了结",
  watch: "issue 追踪",
};
const KIND_CLASS: Record<string, string> = {
  tripwire: "warn",
  breaking: "err",
  release: "ok",
  cve: "err",
  error: "err",
  opportunity: "run",
  "issue-taken": "warn",
  "issue-closed": "mut",
  watch: "ok",
};

export function KindBadge({ kind }: { kind: string }) {
  return (
    <span className={`badge ${KIND_CLASS[kind] || "mut"}`}>{KIND_LABEL[kind] || kind}</span>
  );
}

export function Sha({ sha }: { sha: string | null | undefined }) {
  if (!sha) return <span className="dim">—</span>;
  return <span className="sha">{sha.slice(0, 7)}</span>;
}

export function Empty({ text = "暂无数据" }: { text?: string }) {
  return <div className="empty">{text}</div>;
}

export function Loading() {
  return <div className="load">加载中</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  return <div className="err-box" role="alert">{error instanceof Error ? error.message : String(error)}</div>;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 空间足够时显示一行；仅在日期与时间之间换行，各部分保持完整。 */
export function TimeCell({ iso }: { iso: string | null | undefined }) {
  const s = fmtTime(iso);
  const i = s.indexOf(" ");
  if (i < 0) return <span className="nw">{s}</span>;
  return (
    <span className="g-time-cell">
      <span className="nw">{s.slice(0, i)}</span>{' '}
      <span className="nw">{s.slice(i + 1)}</span>
    </span>
  );
}

/** 页面标题旁的「?」帮助入口（用户 2026-10-02 确立的温和说明入口）：
 * 点开 Modal 看这一页的概念与判定逻辑，界面主体不铺说明文字。 */
export function PageHelp({ title, children }: { title: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="btn ghost"
        style={{ padding: "2px 10px" }}
        title={`${title}？点开看说明`}
        aria-label={`${title}帮助`}
        onClick={() => setOpen(true)}
      >
        ?
      </button>
      {open && (
        <Modal
          title={title}
          onClose={() => setOpen(false)}
          footer={
            <button className="btn" onClick={() => setOpen(false)}>
              知道了
            </button>
          }
        >
          <div className="small" style={{ display: "grid", gap: 14 }}>
            {children}
          </div>
        </Modal>
      )}
    </>
  );
}

/** 弹窗：shadcn Dialog 驱动（焦点圈禁/滚动锁定/焦点归位），视觉保持原 modal-box 体系；
 * 点遮罩不关（防误触丢表单），ESC 关闭；subtitle 渲染在标题下方的身份行 */
export function Modal({
  title,
  subtitle,
  onClose,
  children,
  footer,
  size = 'default',
}: {
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'default' | 'wide';
}) {
  // Radix 无 Trigger 时关闭后焦点会丢到 body：挂载时记住打开者，卸载时还回去
  const openerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    openerRef.current = document.activeElement as HTMLElement | null;
    return () => openerRef.current?.focus?.();
  }, []);
  return (
    <Dialog open onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent
        showCloseButton={false}
        onPointerDownOutside={(e) => e.preventDefault()}
        onInteractOutside={(e) => e.preventDefault()}
        className={`modal-box ${size === 'wide' ? 'modal-wide' : ''} top-[8vh] translate-x-0 translate-y-0 max-w-none sm:max-w-none gap-0 rounded-none border p-0`}
      >
        <span className="tick-b" />
        <div className={`modal-head${subtitle ? " bare" : ""}`}>
          <DialogTitle className="modal-title">{title}</DialogTitle>
          <button className="btn modal-close" aria-label="关闭弹窗" onClick={onClose}>
            ×
          </button>
        </div>
        {subtitle ? <div className="modal-sub">{subtitle}</div> : null}
        <div className="modal-body">{children}</div>
        {footer ? <div className="modal-foot">{footer}</div> : null}
      </DialogContent>
    </Dialog>
  );
}

/** 确认弹窗：替代浏览器系统 confirm()，AlertDialog 语义（Esc/点外均不关，只能明确选择） */
export function Confirm({
  open,
  title,
  body,
  confirmText = "确认",
  danger = false,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmText?: string;
  danger?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <AlertDialogContent
        className="modal-box top-[30vh] translate-x-0 translate-y-0 max-w-none sm:max-w-none gap-0 rounded-none border p-0"
        style={{ top: "30vh", width: "min(420px, calc(100vw - 32px))" }}
      >
        <span className="tick-b" />
        <div className="modal-head">
          <AlertDialogTitle className="modal-title">{title}</AlertDialogTitle>
        </div>
        <div className="modal-body">{body}</div>
        <div className="modal-foot">
          <AlertDialogCancel asChild>
            <button className="btn ghost">取消</button>
          </AlertDialogCancel>
          <AlertDialogAction asChild>
            <button className={danger ? "btn danger" : "btn"} onClick={onConfirm}>
              {confirmText}
            </button>
          </AlertDialogAction>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/** 页码序列：≤7 页全列；再多就 1 … 当前±1 … 末页 */
function pageList(page: number, pages: number): (number | "…")[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const out: (number | "…")[] = [1];
  const from = Math.max(2, page - 1);
  const to = Math.min(pages - 1, page + 1);
  if (from > 2) out.push("…");
  for (let p = from; p <= to; p++) out.push(p);
  if (to < pages - 1) out.push("…");
  out.push(pages);
  return out;
}

/** 标准分页条：总数 · 每页条数（10/25/50）· 页码数字 + 按位置出现的上一页/下一页；
 * 只有一页时整个不渲染。所有列表页统一用这个。 */
/** 每页条数可选项（全站列表页统一） */
export const PAGE_SIZE_OPTIONS = [10, 25, 50];

/** 列表页共用：每页条数——默认 10；用户自己选过就记住（localStorage 按页面分键），不被重置 */
export function usePageSize(key: string): [number, (n: number) => void] {
  const [pageSize, setPageSize] = useState<number>(() => {
    const saved = Number(localStorage.getItem(`gitwire:pageSize:${key}`));
    return PAGE_SIZE_OPTIONS.includes(saved) ? saved : 10;
  });
  const choose = (n: number) => {
    setPageSize(n);
    localStorage.setItem(`gitwire:pageSize:${key}`, String(n));
  };
  return [pageSize, choose];
}

export function ListPager({
  page,
  pages,
  total,
  pageSize,
  onPage,
  onPageSize,
}: {
  page: number;
  pages: number;
  total: number;
  pageSize: number;
  onPage: (p: number) => void;
  onPageSize: (n: number) => void;
}) {
  if (pages <= 1) return null;
  return (
    <div className="pager">
      <span>{total} 条</span>
      <span className="pager-size">
        每页
        <GSelect
          value={String(pageSize)}
          onChange={(v) => onPageSize(Number(v))}
          options={PAGE_SIZE_OPTIONS.map((n) => ({ value: String(n), label: `${n} 条` }))}
          className="min-w-[86px]"
        />
      </span>
      {page > 1 && (
        <button className="btn ghost pager-num" onClick={() => onPage(page - 1)} title="上一页" aria-label="上一页">
          ←
        </button>
      )}
      {pageList(page, pages).map((p, i) =>
        p === "…" ? (
          <span key={`gap${i}`} className="pager-gap">
            …
          </span>
        ) : (
          <button
            key={p}
            className={`btn pager-num${p === page ? "" : " ghost"}`}
            aria-label={`第 ${p} 页`}
            aria-current={p === page ? "page" : undefined}
            onClick={() => onPage(p)}
          >
            {p}
          </button>
        )
      )}
      {page < pages && (
        <button className="btn ghost pager-num" onClick={() => onPage(page + 1)} title="下一页" aria-label="下一页">
          →
        </button>
      )}
    </div>
  );
}

/** 表格占位行：把表格补到本列表见过的整页高度，分页条位置不随行数/换行差异上下漂移。
 * 基准按「路径 + 每页条数」记忆在模块级缓存（翻页时表格被 Loading 替换、组件重挂，缓存不丢）；
 * 只有整页（行数 ≥ pageSize）才刷新基准；展开行（issue-detail-row）不计入，避免撑高基准。 */
const fullPageHeights = new Map<string, number>();

export function FillerRows({ cols, pageSize }: { cols: number; pageSize: number }) {
  const [realH, setRealH] = useState(0);
  const [realCount, setRealCount] = useState(0);
  const anchor = useRef<HTMLTableRowElement | null>(null);

  useEffect(() => {
    const clear = () => fullPageHeights.clear(); // 窗口尺寸变化后行高全变，基准作废重测
    window.addEventListener("resize", clear);
    return () => window.removeEventListener("resize", clear);
  }, []);

  useLayoutEffect(() => {
    const tbody = anchor.current?.closest("tbody");
    if (!tbody) return;
    let h = 0;
    let count = 0;
    for (const tr of Array.from(tbody.children)) {
      if (tr.classList.contains("tbl-filler") || tr.classList.contains("issue-detail-row")) continue;
      h += (tr as HTMLElement).offsetHeight;
      count += 1;
    }
    if (pageSize > 0 && count >= pageSize && h > 0) {
      const key = `${window.location.pathname}:${pageSize}`;
      fullPageHeights.set(key, Math.max(fullPageHeights.get(key) ?? 0, h));
    }
    setRealH((v) => (v === h ? v : h));
    setRealCount((v) => (v === count ? v : count));
  });

  if (pageSize <= 0) return null; // 列表不足一页无分页条，无需占位
  const target = fullPageHeights.get(`${window.location.pathname}:${pageSize}`) ?? 0;
  const fillH = Math.max(0, target - realH);
  return (
    <tr ref={anchor} className="tbl-filler" aria-hidden>
      <td colSpan={cols} style={fillH > 0 ? { height: fillH } : undefined} />
    </tr>
  );
}

/** 筛选条容器 */
export function FilterBar({ children }: { children: ReactNode }) {
  return <div className="filter-bar">{children}</div>;
}
