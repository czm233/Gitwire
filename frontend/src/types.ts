export interface RunBrief {
  id: number;
  repo: string;
  trigger: string;
  mode: string | null;
  old_sha: string | null;
  new_sha: string | null;
  status: "running" | "published" | "failed";
  summary: string | null;
  error: string | null;
  commit_sha: string | null;
  pushed: boolean;
  pr_number: number | null;
  pr_url: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunLog {
  ts: string;
  level: string;
  message: string;
}

export interface RunDetail extends RunBrief {
  logs: RunLog[];
}

export interface AlertItem {
  id: number;
  ts: string | null;
  repo: string;
  kind: string;
  title: string;
  body: string;
  pushed: boolean;
}

export interface RepoRow {
  slug: string;
  repo: string;
  published: boolean;
  sha7: string | null;
  last_status: string | null;
  archived_at: string | null;
  recipes: string[];
  watch_issues: number[];
}

export interface FileEntry {
  path: string;
  size: number;
}

export interface TimelineEntry {
  sha: string;
  date: string;
  subject: string;
  files: { path: string; add: string; del: string }[];
}

export interface DiffFile {
  path: string;
  patch: string;
}

export interface IssueRow {
  repo: string;
  slug: string;
  number: number;
  title: string;
  url: string;
  difficulty: string;
  excluded: string;
  status: string;
  group: "opportunity" | "hard" | "taken" | "closed";
  summary: string;
  problem: string;
  plan: string;
  labels: string[];
  comments: number;
  created_at: string;
  claimed_by: string | null;
  claimed_at: string | null;
  claim_url: string | null;
  assignee: string | null;
  pr_claims: number[];
  pr_evidence: { number: number; url: string; title: string; author: string; created_at: string }[];
  resolved_by: string | null;
  closed_at: string;
  analyzed_at: string;
  watched: boolean;
}

export interface IssueListResp {
  total: number;
  page: number;
  pages: number;
  page_size: number;
  repos: string[];
  stats: { opportunity: number; hard: number; taken: number; closed: number; watched: number };
  items: IssueRow[];
}

export interface RunListResp {
  runs: RunBrief[];
  total: number;
  page: number;
  page_size: number;
}

export interface AlertListResp {
  alerts: AlertItem[];
  total: number;
  page: number;
  page_size: number;
}
