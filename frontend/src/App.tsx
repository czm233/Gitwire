import { QueryClient, QueryClientProvider, useQueryErrorResetBoundary } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Component, ReactNode, useState } from "react";
import { post, UnauthorizedError } from "./api";
import Layout from "./components/Layout";
import Issues from "./pages/Issues";
import ReposManage from "./pages/ReposManage";
import RunsList from "./pages/RunsList";
import RunDetail from "./pages/RunDetail";
import RepoFiles from "./pages/RepoFiles";
import Timeline from "./pages/Timeline";
import Daily from "./pages/Daily";
import Alerts from "./pages/Alerts";
import Settings from "./pages/Settings";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 5000 } },
});

function LoginGate({ onOk }: { onOk: () => void }) {
  const [key, setKey] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="gate">
      <div className="gate-box card">
        <span className="tick-b" />
        <p className="card-title">ACCESS / 访问验证</p>
        <div style={{ display: "flex", gap: 8 }}>
          <input
            className="input"
            type="password"
            style={{ flex: 1 }}
            placeholder="访问密码"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            onKeyDown={async (e) => {
              if (e.key === "Enter") {
                try {
                  await post("/api/login", { key });
                  onOk();
                } catch {
                  setErr("密码不对");
                }
              }
            }}
          />
          <button
            className="btn"
            onClick={async () => {
              try {
                await post("/api/login", { key });
                onOk();
              } catch {
                setErr("密码不对");
              }
            }}
          >
            进入
          </button>
        </div>
        {err && <div className="err-box" style={{ marginTop: 10 }}>{err}</div>}
      </div>
    </div>
  );
}

/** 401 → 登录门；其余错误 → 边界提示 */
class Gate extends Component<{ children: ReactNode }, { gated: boolean; err: unknown }> {
  state = { gated: false, err: null };
  static getDerivedStateFromError(err: unknown) {
    if (err instanceof UnauthorizedError) return { gated: true, err: null };
    return { gated: false, err };
  }
  render() {
    if (this.state.gated) {
      return (
        <LoginGate
          onOk={() => {
            this.setState({ gated: false });
            queryClient.invalidateQueries();
          }}
        />
      );
    }
    if (this.state.err) {
      return (
        <div className="main" style={{ padding: 60 }}>
          <div className="err-box">{String(this.state.err)}</div>
          <p className="dim small" style={{ marginTop: 12 }}>
            后端未启动？<code className="mono">gitwire serve</code>
          </p>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  useQueryErrorResetBoundary();
  return (
    <QueryClientProvider client={queryClient}>
      <Gate>
        <BrowserRouter>
          <Routes>
            <Route element={<Layout />}>
              <Route path="/" element={<Navigate to="/issues" replace />} />
              <Route path="/issues" element={<Issues />} />
              <Route path="/repos" element={<ReposManage />} />
              <Route path="/runs" element={<RunsList />} />
              <Route path="/runs/:id" element={<RunDetail />} />
              <Route path="/repos/:slug" element={<RepoFiles />} />
              <Route path="/repos/:slug/timeline" element={<Timeline />} />
              <Route path="/daily" element={<Daily />} />
              <Route path="/alerts" element={<Alerts />} />
              <Route path="/settings" element={<Settings />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </Gate>
    </QueryClientProvider>
  );
}
