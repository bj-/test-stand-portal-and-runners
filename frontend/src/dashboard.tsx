import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { getJson, type Bug, type HelpPage, type HelpSection, type Job, type Project, type Run, type Stand, type Stats } from "./api";
import { Loaded, Markdown, Severity, formatDate, useLoad } from "./common";
import { RunResult, Trend } from "./pages";
import { ProjectSummariesTile } from "./project-summaries";

const REFRESH_MS = 20000;
const CLOSED = new Set(["closed", "fixed", "verified", "resolved", "rejected", "duplicate", "wontfix", "закрыт", "исправлен"]);
const SEVERITY_ORDER = ["critical", "major", "minor", "trivial"];

const HEALTH: Record<string, { icon: string; label: string; tone: string }> = {
  ok: { icon: "✓", label: "работает", tone: "good" },
  broken: { icon: "✕", label: "не работает", tone: "critical" },
  unknown: { icon: "?", label: "не проверен", tone: "muted" },
};
const JOB: Record<string, { icon: string; tone: string }> = {
  queued: { icon: "○", tone: "muted" },
  running: { icon: "▶", tone: "warning" },
  done: { icon: "✓", tone: "good" },
  failed: { icon: "✕", tone: "critical" },
  cancelled: { icon: "–", tone: "muted" },
};

type Data = { stands: Stand[]; jobs: Job[]; runs: Run[]; stats: Stats; bugs: Bug[] };

/** Загрузка с автообновлением: старые данные остаются на экране, пока идёт новая загрузка. */
function useLive(code: string) {
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    if (!code) return;
    const q = `project=${encodeURIComponent(code)}`;
    const load = () =>
      Promise.all([
        getJson<Stand[]>(`/api/projects/${code}/stands`),
        getJson<{ items: Job[] }>(`/api/jobs?${q}&limit=30`),
        getJson<{ items: Run[] }>(`/api/runs?${q}&limit=8`),
        getJson<Stats>(`/api/stats?${q}&last=30`),
        getJson<Bug[]>(`/api/bugs?${q}`),
      ])
        .then(([stands, jobs, runs, stats, bugs]) => {
          if (!alive.current) return;
          setData({ stands, jobs: jobs.items, runs: runs.items, stats, bugs });
          setError(null);
          setUpdated(new Date());
        })
        .catch((e: Error) => alive.current && setError(e.message));
    load();
    const timer = window.setInterval(load, REFRESH_MS);
    return () => {
      alive.current = false;
      window.clearInterval(timer);
    };
  }, [code]);
  return { data, error, updated };
}

export function DashboardPage({ canRun }: { canRun: boolean }) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [project, setProject] = useState("");
  useEffect(() => {
    getJson<Project[]>("/api/projects").then(setProjects).catch(() => setProjects([]));
  }, []);
  const code = project || projects?.[0]?.code || "";
  const { data, error, updated } = useLive(code);

  return (
    <>
      <header className="page-head">
        <h1>Обзор</h1>
        {(projects?.length ?? 0) > 1 && (
          <select aria-label="Проект" value={code} onChange={(e) => setProject(e.target.value)}>
            {projects?.map((p) => <option key={p.code} value={p.code}>{p.title}</option>)}
          </select>
        )}
        {updated && <span className="sub">обновлено {updated.toLocaleTimeString("ru-RU")}, само раз в 20 с</span>}
        {canRun && <Link className="button" to="/start">Запустить прогон</Link>}
      </header>
      {error && <p className="message error">Не удалось обновить: {error}</p>}
      {projects?.length === 0 && <p className="message">Проектов нет</p>}
      {code && <ProjectSummariesTile key={code} project={code} />}
      {!data && !error && projects?.length !== 0 && <p className="message">Загрузка…</p>}
      {data && <Dashboard data={data} />}
    </>
  );
}

type Tone = "good" | "warning" | "serious" | "critical" | "info" | "muted";

/** Панель в духе Grafana: шапка с названием и ссылкой «подробнее», тело - содержимое. */
function Panel({ title, to, extra, children, className = "" }: {
  title: string; to?: string; extra?: ReactNode; children: ReactNode; className?: string;
}) {
  return (
    <section className={`gpanel ${className}`}>
      <header>
        <h2>{title}</h2>
        {extra}
        {to && <Link className="more" to={to}>подробнее →</Link>}
      </header>
      <div className="gpanel-body">{children}</div>
    </section>
  );
}

/** Стат-плитка: полоса и значок состояния, крупное значение, подпись; цвет - только со значком и словом. */
function StatTile({ title, value, tone, status, sub, to, children }: {
  title: string; value: ReactNode; tone: Tone; status?: string; sub?: ReactNode; to: string; children?: ReactNode;
}) {
  return (
    <Link className={`stat ${tone}`} to={to}>
      <span className="stat-title">{title}</span>
      <span className="stat-value">{value}</span>
      {status && <span className={`stat-status tag ${tone}`}>{status}</span>}
      {children}
      {sub && <span className="stat-sub">{sub}</span>}
    </Link>
  );
}

const ICON: Record<Tone, string> = { good: "✓", warning: "!", serious: "!", critical: "✕", info: "▶", muted: "○" };

/** Доля прошедших по прогонам - мини-график под числом. Подсказка у каждой точки. */
function Sparkline({ points }: { points: Stats["trend"] }) {
  const values = points
    .map((p) => {
      const total = p.passed + p.failed + p.other;
      return { id: p.id, rate: total ? p.passed / total : null };
    })
    .filter((p): p is { id: string; rate: number } => p.rate !== null);
  if (values.length < 2) return null;
  const width = 200, height = 36, pad = 4;
  const x = (i: number) => pad + (i * (width - 2 * pad)) / (values.length - 1);
  const y = (rate: number) => pad + (1 - rate) * (height - 2 * pad);
  const line = values.map((p, i) => `${x(i).toFixed(1)},${y(p.rate).toFixed(1)}`).join(" ");
  return (
    <svg className="spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img"
      aria-label="Доля прошедших тестов по последним прогонам">
      <polyline points={line} />
      {values.map((p, i) => (
        <circle key={p.id} cx={x(i)} cy={y(p.rate)} r={6} className="hit">
          <title>{`${p.id}: ${Math.round(p.rate * 100)} %`}</title>
        </circle>
      ))}
    </svg>
  );
}

function Dashboard({ data }: { data: Data }) {
  const { stands, jobs, runs, stats, bugs } = data;
  const broken = stands.filter((s) => s.health.state === "broken");
  const count = (state: string) => stands.filter((s) => s.health.state === state).length;
  const running = jobs.filter((j) => j.status === "running");
  const queued = jobs.filter((j) => j.status === "queued");
  const open = bugs.filter((b) => !CLOSED.has((b.status ?? "").toLowerCase()));
  const bySeverity = SEVERITY_ORDER.map((name) => ({ name, count: open.filter((b) => b.severity === name).length }))
    .filter((item) => item.count > 0);
  const recentJobs = [...running, ...queued, ...jobs.filter((j) => j.status !== "running" && j.status !== "queued")].slice(0, 8);

  const standsTone: Tone = broken.length ? "critical" : count("unknown") ? "muted" : stands.length ? "good" : "muted";
  const rate = stats.pass_rate;
  const rateTone: Tone = rate === null ? "muted" : rate >= 0.9 ? "good" : rate >= 0.7 ? "warning" : "critical";
  const severities = new Set(bySeverity.map((b) => b.name));
  const bugsTone: Tone = severities.has("critical") ? "critical" : severities.has("major") ? "serious" : open.length ? "warning" : "good";
  const standsStatus = broken.length
    ? `${ICON.critical} не работает: ${broken.length}`
    : count("unknown") ? `? не проверен: ${count("unknown")}` : `${ICON.good} все работают`;
  const rateStatus = rate === null ? undefined
    : `${ICON[rateTone]} ${rateTone === "good" ? "норма" : rateTone === "warning" ? "ниже 90 %" : "ниже 70 %"}`;

  return (
    <div className="dashboard">
      {broken.length > 0 && (
        <div className="alert">
          <strong>✕ Не работает: {broken.map((s) => s.title).join(", ")}</strong>
          <ul>{broken.map((s) => <li key={s.name}><b>{s.title}</b> - {s.health.reasons.join("; ")}</li>)}</ul>
          <Link to="/stands">Подробнее на странице стендов</Link>
        </div>
      )}

      <div className="stats-row">
        <StatTile title="Стенды работают" to="/stands" tone={standsTone}
          value={<>{count("ok")}<small> из {stands.length}</small></>} status={standsStatus} />
        <StatTile title="Задачи сейчас" to="/jobs" tone={running.length ? "info" : "muted"}
          value={running.length}
          status={running.length ? `${ICON.info} выполняется` : "агент свободен"}
          sub={queued.length ? `в очереди: ${queued.length}` : "очередь пуста"} />
        <StatTile title="Прошло тестов" to="/stats" tone={rateTone}
          value={rate === null ? "-" : <>{Math.round(rate * 100)}<small> %</small></>}
          status={rateStatus}
          sub={`последние ${stats.runs} прогонов, ${stats.tests_executed} тестов`}>
          <Sparkline points={stats.trend} />
        </StatTile>
        <StatTile title="Открытые баги" to="/bugs" tone={bugsTone} value={open.length}
          sub={<span className="sev-row">{bySeverity.map((item) => <span key={item.name}><Severity value={item.name} /> {item.count}</span>)}</span>} />
      </div>

      <div className="grid-2">
        <Panel title="Стенды" to="/stands">
          {stands.length === 0 ? <p className="sub">Стенды не описаны</p> : (
            <ul className="rows">{stands.map((s) => <StandRow key={s.name} stand={s} />)}</ul>
          )}
        </Panel>
        <Panel title="Задачи" to="/jobs">
          {recentJobs.length === 0 ? <p className="sub">Задач пока нет</p> : (
            <ul className="rows">
              {recentJobs.map((job) => {
                const s = JOB[job.status] ?? { icon: "?", tone: "muted" };
                return (
                  <li key={job.id} className={`row ${s.tone}`}>
                    <span className={`tag ${s.tone}`}>{s.icon}</span>
                    <div>
                      <Link to={`/jobs/${job.id}`}>{job.title}</Link>
                      <div className="sub">{job.created_by}, {formatDate(job.started ?? job.created)}</div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>
      </div>

      <Panel title="Последние прогоны" to="/runs">
        {runs.length === 0 ? <p className="sub">Прогонов нет</p> : (
          <div className="table-wrap">
            <table className="gtable">
              <thead><tr><th>Прогон</th><th>Набор</th><th>Окружение</th><th>Итог</th><th>Начат</th><th>Кто</th></tr></thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td><Link to={`/runs/${run.id}`}>{run.id}</Link></td>
                    <td>{run.suite}</td>
                    <td className="nowrap">{run.stand_name ?? "-"}</td>
                    <td className="nowrap"><RunResult run={run} /></td>
                    <td className="nowrap">{formatDate(run.started)}</td>
                    <td>{run.who ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      {stats.trend.length > 1 && (
        <Panel title={`Итоги последних ${stats.trend.length} прогонов`} to="/stats" className="flat-chart">
          <Trend points={stats.trend} />
        </Panel>
      )}

      <HelpBlock />
    </div>
  );
}

function StandRow({ stand }: { stand: Stand }) {
  const health = HEALTH[stand.health.state] ?? HEALTH.unknown;
  const versions = (stand.info?.components ?? [])
    .filter((c) => c.name !== "migrations" && (c.api_version || c.version))
    .map((c) => `${c.name} ${c.api_version || c.version}`);
  const last = stand.runs[0];
  return (
    <li className={`row ${health.tone}`}>
      <span className={`tag ${health.tone}`}>{health.icon}</span>
      <div>
        <div className="row-head">
          <Link to="/stands"><b>{stand.title}</b></Link>
          <span className={`tag ${health.tone}`}>{health.label}</span>
          {stand.busy && <span className="tag warning">▶ занят</span>}
        </div>
        <div className="sub">
          {versions.length > 0 ? versions.join(" · ") : "версии неизвестны"}
          {stand.info?.deployed && ` · развёрнут ${formatDate(stand.info.deployed)}`}
        </div>
        {last && (
          <div className="sub">
            прогон <Link to={`/runs/${last.id}`}>{last.suite}</Link>:{" "}
            {last.gate_failed ? "gate не пройден" : `${last.passed} из ${last.tests}`}, {formatDate(last.started)}
          </div>
        )}
      </div>
    </li>
  );
}

/** Документация и полезные ссылки установки: сертификат, инструкции, адреса. Раздел help файла проектов. */
function HelpBlock() {
  const state = useLoad(() => getJson<HelpSection[]>("/api/help"), []);
  if (!state.data || state.data.length === 0) return null;
  return (
    <Panel title="Документация и полезные ссылки" to="/help">
      <div className="help-grid">
        {state.data.map((section) => (
          <div key={section.title}>
            <h3>{section.title}</h3>
            <ul className="plain">
              {section.items.map((item) => (
                <li key={item.title + item.url}>
                  {item.url.startsWith("/help/")
                    ? <Link to={item.url}>{item.title}</Link>
                    : <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a>}
                  {item.note && <div className="sub">{item.note}</div>}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </Panel>
  );
}

export function HelpPageView() {
  const { slug = "" } = useParams();
  const state = useLoad(() => getJson<HelpPage>(`/api/help/pages/${slug}`), [slug]);
  return (
    <Loaded state={state}>
      {(page) => (
        <>
          <p className="links"><Link to="/help#installation">← Справка</Link></p>
          <Markdown>{page.markdown}</Markdown>
        </>
      )}
    </Loaded>
  );
}
