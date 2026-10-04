import { useId, useRef, useState } from 'react';
import { ExternalLink } from 'lucide-react';
import { Modal, fmtTime } from '../components/ui';
import Markdown from '../components/Markdown';
import { stamp } from './core';

const tabs = ['分析', '判断依据', '原始正文'];
const states: Record<string, string> = { open: '开放', 'taken-pr': '有关联 PR', 'taken-assignee': '已指派', 'taken-claim': '评论认领', resolved: '已标记完成', closed: '已关闭' };
const date = (value?: number) => value ? new Date(value * 1000).toLocaleDateString('zh-CN') : '尚无记录';

export default function IssueDetail({ issue, onClose }: { issue: any; onClose: () => void }) {
  const [tab, setTab] = useState(0);
  const id = useId();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const evidence = issue.evidence || [];
  return <Modal size="wide" title={`Issue #${issue.number}`} subtitle={issue.repo} onClose={onClose}
    footer={<><span className="issue-detail-updated">最近检查 · {fmtTime(stamp(issue.checked_at))}</span><a className="btn ghost" href={issue.url} target="_blank" rel="noreferrer">在 GitHub 查看 <ExternalLink size={14} aria-hidden="true" /></a></>}>
    <div className="issue-detail">
      <div className="issue-detail-intro">
        <h2>{issue.title_zh || issue.title}</h2>
        {issue.title_zh && issue.title_zh !== issue.title && <p className="issue-detail-original">{issue.title}</p>}
        <div className="issue-detail-meta"><span className="badge">{states[issue.status] || issue.status}</span><span className="badge" title="AI 根据公开内容估计，实际工作量需人工判断">难度 · {issue.difficulty}</span><span>创建于 {issue.created_at ? new Date(issue.created_at).toLocaleDateString('zh-CN') : '—'}</span></div>
      </div>
      <div className="issue-detail-tabs" role="tablist" aria-label="Issue 详情内容">{tabs.map((name, index) => <button type="button" key={name} role="tab" id={`${id}-tab-${index}`} aria-selected={tab === index} aria-controls={`${id}-panel-${index}`} tabIndex={tab === index ? 0 : -1} ref={el => { tabRefs.current[index] = el; }} onClick={() => setTab(index)} onKeyDown={event => {
        const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
        if (next !== null) { event.preventDefault(); setTab(next); tabRefs.current[next]?.focus(); }
      }}>{name}</button>)}</div>
      {tabs.map((_, index) => <div key={index} className="issue-detail-panel" role="tabpanel" id={`${id}-panel-${index}`} aria-labelledby={`${id}-tab-${index}`} hidden={tab !== index} tabIndex={0}>
        {index === 0 && <>
          {issue.summary && <section className="issue-detail-summary"><h3>问题摘要</h3><p>{issue.summary}</p></section>}
          <section><h3>问题与依据</h3><Markdown text={issue.problem || '尚无分析，可先查看原始正文。'} /></section>
          <section><h3>解决方向</h3><Markdown text={issue.plan || '尚未生成解决方向。'} /></section>
        </>}
        {index === 1 && <>
          <section><h3>机会判断依据</h3><p className="issue-detail-verdict">{issue.excluded || (issue.group === 'opportunity' ? '符合当前机会筛选条件；仍需人工核实。' : '当前状态或难度尚未满足入榜条件。')}</p><dl className="issue-detail-facts"><div><dt>仓库最近提交</dt><dd>{date(issue.repo_signals?.pushed_at)}</dd></div><div><dt>最近外部 PR 合并</dt><dd>{issue.repo_signals?.external_merge_at ? date(issue.repo_signals.external_merge_at) : '尚未发现'}</dd></div><div><dt>仓库证据采集</dt><dd>{issue.repo_signals?.checked_at ? fmtTime(stamp(issue.repo_signals.checked_at)) : '待更新'}</dd></div></dl></section>
          <section><h3>状态证据</h3>{evidence.length ? <ul className="issue-detail-evidence">{evidence.map((e: any, index: number) => <li key={index}><p>{e.reason}</p>{e.kind === 'pr' && <p>{e.repo}#{e.number} · {e.merged ? '已合并' : e.open ? '仍开放' : '已关闭未合并'}{e.author ? ` · ${e.author}` : ''}</p>}{e.url && <a href={e.url} target="_blank" rel="noreferrer">查看依据 ↗</a>}</li>)}</ul> : <p>尚无指派或明确修复 PR 的证据，不代表已确认无人处理。</p>}
          {issue.claim?.author && <div className="issue-detail-claim"><p>{issue.claim.author} · {issue.claim.reason}</p><p>有效至 {fmtTime(stamp(issue.claim.until))} · <a href={issue.claim.url} target="_blank" rel="noreferrer">查看认领原文 ↗</a></p></div>}</section>
        </>}
        {index === 2 && <Markdown text={issue.body || '暂无原始正文。'} />}
      </div>)}
    </div>
  </Modal>;
}
