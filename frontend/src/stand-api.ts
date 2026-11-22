/** Публичные проекции каталога: команды, секреты и пути clones сюда не входят. */
export type StandRepository = { key: string; title: string; components: string[]; enabled: boolean; git_url: string | null; git_server_id: string | null };
export type StandRepositoryPreview = { git_url: string; key: string; title: string; components: string[] };
export type GitServer = { id: string; title: string; base_url: string; provider: "gitlab" | "github" | "other"; has_token: boolean; username: string; access: "git" | "api" };
export type GitRemoteRepository = { name: string; path: string; git_url: string };
export type GitRepositoryPage = { items: GitRemoteRepository[]; page: number; has_next: boolean };
export type StandParameter = { type: "string" | "integer" | "number" | "boolean"; default?: string | number | boolean; enum?: (string | number | boolean)[]; required?: boolean; title?: string; description?: string };
export type StandDataModule = { title: string; service: string; format: string };
export type StandDataEntry = { module: string; dataset_id: number; version: number; title: string; key: string; format: string };
export type StandDataBundle = { title: string; entries: StandDataEntry[] };
export type StandTemplate = {
  id: string; version: string; title: string;
  components: { id: string; repository: string; default_ref: string; dependencies: string[] }[];
  profiles: Record<string, { services: string[]; resources: { cpus: number; memory_mb: number } }>;
  parameters: Record<string, StandParameter>;
  output_binding_titles?: Record<string, string>;
  name_ref_exclusions?: string[];
  technical_components?: string[];
  data_modules?: Record<string, StandDataModule>;
};
export type StandDeploymentConfiguration = {
  stand: string; title: string; refs: { name: string; title: string; default: string }[];
};
export type StandHost = { id: string; title: string; port_range: [number, number] };
export type StandRef = { name: string; kind: "branch" | "tag" };
export type StandNameCheck = { conflict: { name: string; suggested_name: string } | null };
export type StandCreateRequest = {
  name: string; template_key: string; template_version: string; target_host: string; profile: string;
  components: Record<string, { repository?: string; ref?: string }>;
  parameters: Record<string, string | number | boolean>;
  environments: string[]; mode: "permanent" | "ephemeral"; expires: string | null;
  ports: Record<string, number>;
  install_test_data: boolean;
};
export type StandPreflight = {
  valid: boolean;
  components: { id: string; repository: string; requested_ref: string; observed_sha: string }[];
  resources: { cpus: number; memory_mb: number }; ports: Record<string, number>;
  executor_available: boolean; warnings: string[];
  output_binding_titles?: Record<string, string>;
  data_bundle?: StandDataBundle | null;
};
export type StandInstance = {
  id: string; name: string; project: string; owner_login: string;
  status: "created" | "waiting" | "building" | "deploying" | "checking" | "ready" | "limited" | "failed" | "stopped" | "deleting" | "deleted";
  target_host: string; template_key: string; template_version: string; profile: string;
  environments: string[]; mode: "permanent" | "ephemeral"; expires: string | null;
  job_id: number | null; job_status: string | null; job_message: string | null;
  ports: Record<string, number>; deployed_build_id: string | null; waiting_reason: string | null;
  output_binding_titles?: Record<string, string>;
  deletion_job_id?: number | null; deletion_status?: "queued" | "running" | "done" | "failed" | "cancelled" | null;
  deletion_message?: string | null; deleted_at?: string | null; can_delete?: boolean;
  data_bundle?: StandDataBundle | null;
};
export type StandBuild = {
  id: string; project: string; stand_id: string; job_id: number | null;
  status: string; created: string; started: string | null; finished: string | null;
  template_key: string; template_version: string; profile: string | null;
  components: { id: string; repository: string; requested_ref: string; sha: string }[];
  stages: { name: string; status: string; started_at?: string; finished_at?: string }[];
  images: { id: string; repo_digests: string[]; repo_tags: string[] }[];
  output_bindings: Record<string, string>; output_binding_titles?: Record<string, string>; artifacts: { name: string; url: string }[];
};

export function bindingTitle(name: string, titles?: Record<string, string>): string {
  return titles?.[name] || ({ baseUrl: "Основной адрес", adminUrl: "Панель управления" } as Record<string, string>)[name] || name;
}

export const projectApi = (code: string) => `/api/projects/${encodeURIComponent(code)}`;
export function newIdempotencyKey(): string {
  if (globalThis.crypto?.randomUUID) return `ui-${globalThis.crypto.randomUUID()}`;
  const bytes = new Uint8Array(16);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
    return `ui-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  }
  // Это уникальный ключ операции, а не секрет или средство авторизации.
  return `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}
