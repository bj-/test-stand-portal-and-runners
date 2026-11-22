import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getJson, getText, type Bug, type BugDetail, type Project, type Run, type RunDetail, type Stats, type TrendPoint } from "./api";
import { Loaded, Markdown, Severity, Verdict, formatDate, useLoad } from "./common";

function ProjectFilter() {
  const [params, setParams] = useSearchParams();
  const projects = useLoad(() => getJson<Project[]>("/api/projects"), []);
  if (!projects.data || projects.data.length < 2) return null;
  return (
    <select
      aria-label="Проект"
      value={params.get("project") ?? ""}
      onChange={(e) => setParams(e.target.value ? { project: e.target.value } : {})}
    >
      <option value="">Все проекты</option>
      {projects.data.map((p) => (
        <option key={p.code} value={p.code}>
          {p.title}
        </option>
      ))}
    </select>
  );
}

export function RunResult({ run }: { run: Run }) {
  return (
    <span className="numbers">
      <b>{run.passed}</b> из {run.tests}
      {run.failed > 0 && <span className="tag critical">✕ {run.failed}</span>}
      {run.gate_failed && <span className="tag serious">! smoke</span>}
    </span>
  );
}

export function RunsPage() {
  const [params] = useSearchParams();
  const project = params.get("project") ?? "";
  const state = useLoad(
    () => getJson<{ total: number; items: Run[] }>(`/api/runs?limit=200${project ? `&project=${project}` : ""}`),
    [project],
  );
  return (
    <>
      <header className="page-head">
        <h1>Прогоны</h1>
        <ProjectFilter />
      </header>
      <Loaded state={state}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="message">Прогонов пока нет. Выполните импорт: python -m portal.cli sync</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Прогон</th>
                    <th>Начало</th>
                    <th>Стенд</th>
                    <th>Результат</th>
                    <th>Кто</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((run) => (
                    <tr key={run.id}>
                      <td>
                        <Link to={`/runs/${run.id}`}>{run.suite}</Link>
                        <div className="sub">{run.modules.join(", ") || run.project}</div>
                      </td>
                      <td className="nowrap">{formatDate(run.started)}</td>
                      <td>{run.stand_name ?? run.stand_url ?? "-"}</td>
                      <td className="nowrap">
                        <RunResult run={run} />
                      </td>
                      <td>{run.who ?? "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </Loaded>
    </>
  );
}

const FILTERS = [
  ["all", "Все"],
  ["failed", "Упавшие"],
  ["passed", "Прошедшие"],
] as const;

function RunResults({ run }: { run: RunDetail }) {
  const [filter, setFilter] = useState<string>(run.failed > 0 ? "failed" : "all");
  const rows = useMemo(
    () => run.results.filter((r) => filter === "all" || r.verdict === filter),
    [run.results, filter],
  );
  return (
    <>
      <div className="segments" role="group" aria-label="Фильтр тестов">
        {FILTERS.map(([value, label]) => (
          <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>
            {label}
          </button>
        ))}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Тест</th>
              <th>Запрос</th>
              <th>Код</th>
              <th>Время</th>
              <th>Итог</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.position}>
                <td className="wide">{r.path}</td>
                <td className="nowrap">{r.method ?? "-"}</td>
                <td>{r.code ?? "-"}</td>
                <td className="nowrap">{r.time_ms === null ? "-" : `${r.time_ms} мс`}</td>
                <td className="nowrap">
                  <Verdict value={r.verdict} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

function RunReport({ id }: { id: string }) {
  const state = useLoad(() => getText(`/api/runs/${id}/report`), [id]);
  return <Loaded state={state}>{(text) => <Markdown>{text}</Markdown>}</Loaded>;
}

export function RunPage() {
  const { id = "" } = useParams();
  const state = useLoad(() => getJson<RunDetail>(`/api/runs/${id}`), [id]);
  const [tab, setTab] = useState("tests");
  return (
    <Loaded state={state}>
      {(run) => (
        <>
          <header className="page-head">
            <div>
              <h1>{run.suite}</h1>
              <div className="sub">{run.id}</div>
            </div>
          </header>
          <dl className="facts">
            <div><dt>Результат</dt><dd><RunResult run={run} /></dd></div>
            <div><dt>Проверки</dt><dd>{run.checks - run.checks_failed} из {run.checks}</dd></div>
            <div><dt>Стенд</dt><dd>{run.stand_name ?? "-"}<div className="sub">{run.stand_url}</div></dd></div>
            <div><dt>Сборка</dt><dd>{run.build ?? "-"}</dd></div>
            <div><dt>Тесты</dt><dd>{run.tests_branch ?? "-"} {run.tests_commit}</dd></div>
            <div><dt>Начало</dt><dd>{formatDate(run.started)}</dd></div>
            <div><dt>Кто</dt><dd>{run.who ?? "-"}</dd></div>
            <div><dt>Исполнитель</dt><dd>{run.runner ?? "-"}</dd></div>
          </dl>
          {run.bug_ids.length > 0 && (
            <p className="links">
              Баги: {run.bug_ids.map((b) => <Link key={b} to={`/bugs/${b}`}>{b}</Link>)}
            </p>
          )}
          <div className="segments" role="tablist">
            <button role="tab" aria-selected={tab === "tests"} onClick={() => setTab("tests")}>Тесты</button>
            <button role="tab" aria-selected={tab === "report"} disabled={!run.has_report} onClick={() => setTab("report")}>Отчёт</button>
            {run.has_allure ? (
              <a href={`/api/runs/${run.id}/allure`} target="_blank" rel="noreferrer">Allure</a>
            ) : (
              <span className="sub" title="Allure хранится вне git и в этой копии отсутствует">Allure нет</span>
            )}
          </div>
          {tab === "tests" ? <RunResults run={run} /> : <RunReport id={run.id} />}
        </>
      )}
    </Loaded>
  );
}

export function BugsPage() {
  const [params] = useSearchParams();
  const project = params.get("project") ?? "";
  const [severity, setSeverity] = useState("");
  const state = useLoad(() => getJson<Bug[]>(`/api/bugs${project ? `?project=${project}` : ""}`), [project]);
  return (
    <>
      <header className="page-head">
        <h1>Баги</h1>
        <ProjectFilter />
        <select aria-label="Серьёзность" value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="">Любая серьёзность</option>
          <option value="critical">critical</option>
          <option value="major">major</option>
          <option value="minor">minor</option>
          <option value="trivial">trivial</option>
        </select>
      </header>
      <Loaded state={state}>
        {(bugs) => (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Номер</th>
                  <th>Заголовок</th>
                  <th>Серьёзность</th>
                  <th>Статус</th>
                  <th>Найден</th>
                </tr>
              </thead>
              <tbody>
                {bugs
                  .filter((b) => !severity || b.severity === severity)
                  .map((bug) => (
                    <tr key={bug.id}>
                      <td className="nowrap"><Link to={`/bugs/${bug.id}`}>{bug.id}</Link></td>
                      <td className="wide">
                        {bug.title}
                        {bug.restricted && <span className="tag muted">закрытый</span>}
                      </td>
                      <td><Severity value={bug.severity} /></td>
                      <td>{bug.status ?? "-"}</td>
                      <td className="nowrap">{bug.found_date ?? "-"}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </Loaded>
    </>
  );
}

export function BugPage() {
  const { id = "" } = useParams();
  const state = useLoad(() => getJson<BugDetail>(`/api/bugs/${id}`), [id]);
  return (
    <Loaded state={state}>
      {(bug) => (
        <>
          {bug.run_id && <p className="links">Прогон: <Link to={`/runs/${bug.run_id}`}>{bug.run_id}</Link></p>}
          <Markdown>{bug.body}</Markdown>
        </>
      )}
    </Loaded>
  );
}

const CHART = { width: 760, height: 220, left: 34, right: 8, top: 10, bottom: 8 };

/** Высота столбца - число тестов, поэтому разный объём наборов виден сразу, а доли читаются по сегментам. */
export function Trend({ points }: { points: TrendPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const max = Math.max(1, ...points.map((p) => p.passed + p.failed + p.other));
  const plot = CHART.height - CHART.top - CHART.bottom;
  const step = (CHART.width - CHART.left - CHART.right) / points.length;
  const bar = Math.min(28, Math.max(4, step - 4));
  const y = (value: number) => (value / max) * plot;
  const baseline = CHART.height - CHART.bottom;
  const active = hover === null ? null : points[hover];
  return (
    <figure className="chart">
      <div className="legend">
        <span><i className="swatch pass" /> прошли</span>
        <span><i className="swatch critical" /> упали</span>
        <span><i className="swatch muted" /> прочее</span>
      </div>
      <div className="plot">
        <svg viewBox={`0 0 ${CHART.width} ${CHART.height}`} role="img" aria-label="Итоги последних прогонов" onMouseLeave={() => setHover(null)}>
          {[0, 0.5, 1].map((share) => (
            <g key={share}>
              <line className={share === 0 ? "axis" : "grid"} x1={CHART.left} x2={CHART.width - CHART.right} y1={baseline - share * plot} y2={baseline - share * plot} />
              <text className="tick" x={CHART.left - 6} y={baseline - share * plot + 4} textAnchor="end">{Math.round(max * share)}</text>
            </g>
          ))}
          {points.map((point, index) => {
            const x = CHART.left + index * step + (step - bar) / 2;
            let top = baseline;
            const segments = (
              [["pass", point.passed], ["critical", point.failed], ["muted", point.other]] as const
            ).filter(([, value]) => value > 0);
            return (
              <g key={point.id} onMouseEnter={() => setHover(index)}>
                <rect className="hit" x={CHART.left + index * step} y={CHART.top} width={step} height={plot} />
                {segments.map(([tone, value], i) => {
                  const height = Math.max(1, y(value) - (i > 0 ? 2 : 0));
                  top -= height + (i > 0 ? 2 : 0);
                  return <rect key={tone} className={`fill ${tone}`} x={x} y={top} width={bar} height={height} rx={i === segments.length - 1 ? 2 : 0} />;
                })}
              </g>
            );
          })}
        </svg>
        {active && hover !== null && (
          <div className="tooltip" style={{ left: `${((CHART.left + (hover + 0.5) * step) / CHART.width) * 100}%` }}>
            <b>{active.suite}</b>
            <div className="sub">{formatDate(active.started)}</div>
            <div>✓ прошли: {active.passed}</div>
            <div>✕ упали: {active.failed}</div>
            {active.other > 0 && <div>○ прочее: {active.other}</div>}
          </div>
        )}
      </div>
      <details>
        <summary>Таблица</summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th>Прогон</th><th>Прошли</th><th>Упали</th><th>Прочее</th></tr></thead>
            <tbody>
              {points.map((p) => (
                <tr key={p.id}><td><Link to={`/runs/${p.id}`}>{p.id}</Link></td><td>{p.passed}</td><td>{p.failed}</td><td>{p.other}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}

export function StatsPage() {
  const [params] = useSearchParams();
  const project = params.get("project") ?? "";
  const state = useLoad(() => getJson<Stats>(`/api/stats${project ? `?project=${project}` : ""}`), [project]);
  return (
    <>
      <header className="page-head">
        <h1>Статистика</h1>
        <ProjectFilter />
      </header>
      <Loaded state={state}>
        {(stats) => (
          <>
            <div className="tiles">
              <div><span>Прошло тестов</span><b>{stats.pass_rate === null ? "-" : `${Math.round(stats.pass_rate * 100)} %`}</b></div>
              <div><span>Прогонов</span><b>{stats.runs}</b></div>
              <div><span>Выполнено тестов</span><b>{stats.tests_executed}</b></div>
              <div>
                <span>Баги</span>
                <b>{stats.bugs_by_severity.reduce((sum, item) => sum + item.count, 0)}</b>
                <div className="tag-row">
                  {stats.bugs_by_severity.map((item) => <span key={item.name}><Severity value={item.name} /> {item.count}</span>)}
                </div>
              </div>
            </div>
            <h2>Последние прогоны</h2>
            <Trend points={stats.trend} />
            <h2>Чаще всего падают</h2>
            <FailingTable items={stats.top_failing} />
            <h2>Нестабильные</h2>
            <p className="sub">Итог менялся не меньше двух раз подряд идущими прогонами</p>
            <FailingTable items={stats.unstable} />
          </>
        )}
      </Loaded>
    </>
  );
}

function FailingTable({ items }: { items: Stats["top_failing"] }) {
  if (items.length === 0) return <p className="message">Нет данных</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>Тест</th><th>Упал</th><th>Из прогонов</th></tr></thead>
        <tbody>
          {items.map((t) => (
            <tr key={t.path}><td className="wide">{t.path}</td><td>{t.failed}</td><td>{t.runs}</td></tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
