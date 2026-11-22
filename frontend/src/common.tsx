import { useEffect, useState, type ReactNode } from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";

export function useLoad<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    load()
      .then((value) => active && setData(value))
      .catch((e: Error) => active && setError(e.message));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error };
}

export function Loaded<T>({ state, children }: { state: { data: T | null; error: string | null }; children: (data: T) => ReactNode }) {
  if (state.error) return <p className="message error">Не удалось загрузить: {state.error}</p>;
  if (state.data === null) return <p className="message">Загрузка…</p>;
  return <>{children(state.data)}</>;
}

export const formatDate = (value: string | null) =>
  value ? new Date(value).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "-";

const VERDICTS: Record<string, { icon: string; label: string; tone: string }> = {
  passed: { icon: "✓", label: "прошёл", tone: "good" },
  failed: { icon: "✕", label: "упал", tone: "critical" },
  error: { icon: "!", label: "ошибка", tone: "serious" },
  "no-checks": { icon: "○", label: "без проверок", tone: "muted" },
  skipped: { icon: "–", label: "пропущен", tone: "muted" },
};

export function Verdict({ value }: { value: string }) {
  const verdict = VERDICTS[value] ?? { icon: "?", label: value, tone: "muted" };
  return (
    <span className={`tag ${verdict.tone}`}>
      {verdict.icon} {verdict.label}
    </span>
  );
}

const SEVERITY_TONE: Record<string, string> = { critical: "critical", major: "serious", minor: "warning", trivial: "muted" };

export function Severity({ value }: { value: string | null }) {
  return <span className={`tag ${SEVERITY_TONE[value ?? ""] ?? "muted"}`}>{value ?? "-"}</span>;
}

/** Файлы результатов ссылаются друг на друга относительными путями репозитория; в портале это страницы. */
function portalUrl(url: string) {
  const run = url.match(/([A-Z0-9]+-\d{8}-\d{6}-[^/]+)\/report\.md/);
  if (run) return `/runs/${run[1]}`;
  const bug = url.match(/(BUG-[A-Z0-9]+-\d{4})[^/]*\.md$/);
  if (bug) return `/bugs/${bug[1]}`;
  return defaultUrlTransform(url);
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} urlTransform={portalUrl}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
