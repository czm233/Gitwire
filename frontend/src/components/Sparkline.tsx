/** 零依赖内联 SVG sparkline（CZM-33 方案 2.4 选型：不引图表库）。
 * 组件预留：FR-1 内不接数据——/issues 只有当前时点 stats 快照，无时序接口，
 * 不渲染假趋势（PM 决议 3）；趋势能力另行立项后即插即用。 */
export default function Sparkline({ values, tone = "cyan", title }: {
  values: number[];
  tone?: "cyan" | "green" | "amber" | "dim" | "purple";
  title?: string;
}) {
  if (values.length < 2) return null;
  const w = 104, h = 30, pad = 2;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const points = values.map((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = pad + (1 - (v - min) / span) * (h - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = points[points.length - 1].split(",");
  return (
    <svg className="spark" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" role="img" aria-label={title}>
      {title && <title>{title}</title>}
      <polyline className={`s-${tone}`} points={points.join(" ")} />
      <circle className={`f-${tone}`} cx={last[0]} cy={last[1]} r="2.2" />
    </svg>
  );
}
