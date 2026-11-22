export type Run = {
  id: string;
  project: string;
  suite: string;
  kind: string | null;
  modules: string[];
  started: string | null;
  finished: string | null;
  who: string | null;
  trigger: string | null;
  stand_name: string | null;
  stand_url: string | null;
  stand_kind: string | null;
  build: string | null;
  tests_branch: string | null;
  tests_commit: string | null;
  engine_version: string | null;
  runner: string | null;
  gate_failed: boolean;
  tests: number;
  passed: number;
  failed: number;
  errors: number;
  no_checks: number;
  skipped: number;
  checks: number;
  checks_failed: number;
  bug_ids: string[];
};

export type TestResult = {
  position: number;
  path: string;
  name: string;
  method: string | null;
  url: string | null;
  code: number | null;
  time_ms: number | null;
  verdict: string;
  gate: boolean;
  groups: string[];
};

export type RunDetail = Run & { results: TestResult[]; has_report: boolean; has_allure: boolean };

export type Bug = {
  id: string;
  project: string;
  number: number;
  title: string;
  module: string | null;
  type: string | null;
  severity: string | null;
  priority: string | null;
  status: string | null;
  restricted: boolean;
  groups: string[];
  stand: string | null;
  found_date: string | null;
  run_id: string | null;
  tracker: string | null;
};

export type BugDetail = Bug & { body: string };

export type TrendPoint = { id: string; started: string | null; suite: string; passed: number; failed: number; other: number };
export type CountItem = { name: string; count: number };
export type FailingTest = { path: string; failed: number; runs: number };

export type Stats = {
  runs: number;
  tests_executed: number;
  pass_rate: number | null;
  trend: TrendPoint[];
  bugs_by_severity: CountItem[];
  bugs_by_status: CountItem[];
  top_failing: FailingTest[];
  unstable: FailingTest[];
};

export type Project = { code: string; title: string };

export type Me = { name: string; role: string; is_guest: boolean; must_change_password: boolean; password_change_suggested: boolean; theme?: string };
export type PortalUser = {
  login: string;
  name: string;
  role: string;
  active: boolean;
  email?: string;
  telegram?: string;
  last_login?: string | null;
  locked?: boolean;
  theme?: string;
  password?: string | null;
};

export type Suite = { path: string; description: string; destructive: boolean };
export type RunStand = { id: string; name: string; status: "ready" | "limited"; bindings: { id: string; title: string }[] };
export type Catalog = { suites: Suite[]; environments: string[]; legacy_environments?: string[]; run_stands?: RunStand[] };
export type TestCatalog = { runner: string; ref: string; commit: string | null; gate: string | null; destructive: boolean; items: { id: string; label: string }[] };
export type Job = {
  id: number;
  project: string;
  kind: string;
  status: string;
  target: string;
  title: string;
  created_by: string;
  created: string;
  started: string | null;
  finished: string | null;
  agent: string | null;
  run_id: string | null;
  message: string | null;
  cancel_requested: boolean;
  batch_id: string | null;
};
export type JobDetail = Job & { log: string };
export type JobBatch = { id: string; project: string; created_by: string; created: string; total: number; finished: number; status: string; counts: { queued: number; running: number; done: number; failed: number; cancelled: number }; jobs: Job[] };

export type StandAction = { name: string; title: string; destructive: boolean; allowed: boolean };

export function getStand(project: string, name: string): Promise<Stand | null> {
  return getJson<Stand[]>(`/api/projects/${encodeURIComponent(project)}/stands`)
    .then((stands) => stands.find((stand) => stand.name === name) ?? null);
}
export type StandComponent = {
  name: string;
  ref?: string;
  commit?: string;
  commit_date?: string;
  version?: string;
  api_version?: string | null;
  subject?: string;
};
export type StandInfo = {
  built?: string | null;
  built_on?: string | null;
  deployed?: string | null;
  checked?: string | null;
  components?: StandComponent[];
  migrations?: { applied?: number; last?: string | null; in_build?: number };
};
export type StandCheck = { name: string; ok: boolean; detail: string };
export type StandRun = {
  id: string;
  env: string;
  suite: string;
  kind: string | null;
  started: string | null;
  tests: number;
  passed: number;
  failed: number;
  errors: number;
  gate_failed: boolean;
};
export type Stand = {
  name: string;
  title: string;
  source?: "yaml" | "database";
  mode?: "permanent" | "ephemeral" | null;
  expires?: string | null;
  can_run?: boolean;
  state?: string | null;
  description: string;
  problem: string;
  seed_info: string;
  can_view_test_data: boolean;
  environments: string[];
  links: { title: string; url: string }[];
  access: { title: string; value: string; secret: boolean }[] | null;
  info: StandInfo | null;
  health: { state: "ok" | "limited" | "broken" | "unknown"; reasons: string[]; checks: StandCheck[]; checked: string | null };
  runs: StandRun[];
  params: { name: string; title: string; default: string }[];
  actions: StandAction[];
  busy: boolean;
  jobs: { id: number; title: string; status: string; created_by: string; created: string; finished: string | null }[];
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(url: string, init: RequestInit | undefined, parse: (r: Response) => Promise<T>): Promise<T> {
  const response = await fetch(url, init);
  if (response.status === 401 && !url.startsWith("/api/auth/")) window.dispatchEvent(new Event("portal:signed-out"));
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const validation = Array.isArray(detail) ? detail.filter((item) => typeof item?.msg === "string")
      .map((item) => `${Array.isArray(item.loc) ? item.loc.filter((part: unknown) => part !== "body").join(".") + ": " : ""}${item.msg}`).join("; ") : "";
    throw new ApiError(response.status, typeof detail === "string" ? detail : validation || `${response.status}: ${url}`);
  }
  return parse(response);
}

export const getJson = <T,>(url: string) => request<T>(url, undefined, (r) => r.json());
export const getText = (url: string) => request<string>(url, undefined, (r) => r.text());

/** Preserve numeric literals in payloads that have already been validated as JSON. */
export const sendRawJson = <T,>(url: string, method: "POST" | "PUT", body: string) =>
  request<T>(url, { method, headers: { "Content-Type": "application/json" }, body },
    (r) => r.status === 204 ? Promise.resolve(undefined as T) : r.json());

export const sendJson = <T,>(url: string, method: "POST" | "PATCH" | "PUT" | "DELETE", body?: unknown, headers?: Record<string, string>) =>
  request<T>(
    url,
    { method, headers: { "Content-Type": "application/json", ...headers }, body: body === undefined ? undefined : JSON.stringify(body) },
    (r) => (r.status === 204 ? Promise.resolve(undefined as T) : r.json()),
  );

export type HelpItem = { title: string; url: string; note: string };
export type HelpSection = { title: string; items: HelpItem[] };
export type HelpPage = { title: string; markdown: string };

export type DatasetSummary = { id: number; key: string; title: string; description: string; format: string; version: number; created_by: string; created: string; can_edit: boolean };
export type DatasetVersion = { version: number; created_by: string; created: string };
export type DatasetDetail = DatasetSummary & { versions: DatasetVersion[] };
export type DatasetContent = DatasetVersion & { data: Record<string, unknown> };
export type DataTarget = { name: string; title: string; format: string; can_apply: boolean; can_inspect: boolean };
export type DataBinding = { id: number; dataset_id: number; version: number; target: string; enabled: boolean; auto_apply: boolean; position: number; title: string; key: string; format: string };
export type StandTestData = { targets: DataTarget[]; bindings: DataBinding[]; can_manage: boolean };
export type DataJob = { id: number; status: string; message: string | null; result: Record<string, unknown> | null };
