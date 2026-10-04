import { NavLink, Outlet } from "react-router-dom";

const NAV = [
  { to: "/issues", label: "Issue 雷达", idx: "01" },
  { to: "/repos", label: "监控清单", idx: "02" },
  { to: "/runs", label: "运行历史", idx: "03" },
  { to: "/daily", label: "每日晨报", idx: "04" },
  { to: "/alerts", label: "警报", idx: "05" },
  { to: "/settings", label: "设置", idx: "06" },
];

export default function Layout() {
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          GITWIRE<span className="cursor" />
        </div>
        <div className="brand-sub">OSS INTEL DESK</div>
        {NAV.map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
          >
            <span className="idx">{n.idx}</span>
            {n.label}
          </NavLink>
        ))}
        <div className="side-footer">
          WATCH → ANALYZE → PUBLISH
          <br />
          编排归代码 · 语义归 agent
        </div>
      </aside>
      <main className="main">
        <Outlet />
        <div className="footer-note">GITWIRE // 一个人的开源情报站</div>
      </main>
    </div>
  );
}
