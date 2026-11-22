import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getJson, sendJson, type Catalog, type TestCatalog, type Job, type JobDetail, type JobBatch, type Project } from "./api";
import { Loaded, formatDate, useLoad } from "./common";
import { ConfirmDialog } from "./confirm";
import { TestTree } from "./test-tree";
import { SuiteTree } from "./suite-tree";

const STATUS: Record<string, { icon: string; label: string; tone: string }> = {
  queued: { icon: "○", label: "в очереди", tone: "muted" },
  running: { icon: "▶", label: "выполняется", tone: "warning" },
  done: { icon: "✓", label: "завершена", tone: "good" },
  failed: { icon: "✕", label: "сбой", tone: "critical" },
  cancelled: { icon: "–", label: "отменена", tone: "muted" },
};

function Status({ value }: { value: string }) {
  const status = STATUS[value] ?? { icon: "?", label: value, tone: "muted" };
  return <span className={`tag ${status.tone}`}>{status.icon} {status.label}</span>;
}

const active = (status: string) => status === "queued" || status === "running";

function JobDuration({ job }: { job: Job }) {
  if (job.status === "queued" || job.status === "pending") {
    return <span title="Ещё не запущена">—</span>;
  }
  const started = job.started ? Date.parse(job.started) : NaN;
  const finished = job.finished ? Date.parse(job.finished) : job.status === "running" ? Date.now() : NaN;
  if (!Number.isFinite(started) || !Number.isFinite(finished) || finished < started) {
    return <span title="Длительность недоступна: нет корректного времени начала или завершения">—</span>;
  }
  const seconds = Math.floor((finished - started) / 1000);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds / 3600) % 24;
  const minutes = Math.floor(seconds / 60) % 60;
  const compact = seconds < 60 ? "<1m" : [
    days > 0 ? `${days}d` : "",
    hours > 0 ? `${hours}h` : "",
    days === 0 && minutes > 0 ? `${minutes}m` : "",
  ].filter(Boolean).join(" ");
  const full = [
    days > 0 ? `${days}d` : "",
    `${hours}h`, `${minutes}m`, `${seconds % 60}s`,
  ].filter(Boolean).join(" ");
  return <span title={full}>{compact}</span>;
}

function usePolling<T>(url: string, stop: (value: T) => boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const stopRef = useRef(stop);
  stopRef.current = stop;
  useEffect(() => {
    let alive = true;
    let timer = 0;
    const tick = () =>
      getJson<T>(url)
        .then((value) => {
          if (!alive) return;
          setData(value);
          setError(null);
          if (!stopRef.current(value)) timer = window.setTimeout(tick, 2000);
        })
        .catch((e: Error) => alive && setError(e.message));
    tick();
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [url]);
  return { data, error };
}

export function StartPage({ role }: { role: string }) {
  const [params] = useSearchParams();
  const query = params.toString();
  const requestedProject = params.get("project") || "";
  const requestedStand = params.get("stand");
  const projects = useLoad(() => getJson<Project[]>("/api/projects"), []);
  const [project, setProject] = useState({ query, code: requestedProject });
  const code = (project.query === query ? project.code : requestedProject) || projects.data?.[0]?.code || "";
  // useLoad сохраняет предыдущие данные до запуска эффекта: проверяем их проект.
  const catalogLoad = useLoad(async () => ({ code, data: code ? await getJson<Catalog>(`/api/projects/${encodeURIComponent(code)}/catalog`) : null }), [code]);
  const catalog = { ...catalogLoad, data: catalogLoad.data?.code === code ? catalogLoad.data : null };
  const data = catalog.data?.data;
  const targetKey = JSON.stringify([code, query]);
  const [target, setTarget] = useState<{ key: string; value: string; message?: string }>({ key: "", value: "" });
  const legacyEnvironments = data?.legacy_environments ?? data?.environments ?? [];
  const runStands = data?.run_stands ?? [];
  const legacyValue = (env: string) => JSON.stringify(["legacy", env]);
  const managedValue = (stand: string, binding: string) => JSON.stringify(["managed", stand, binding]);
  const targetValue = target.key === targetKey ? target.value : "";
  const selectedStand = runStands.find((stand) => stand.bindings.some((binding) => managedValue(stand.id, binding.id) === targetValue));
  const selectedBinding = selectedStand?.bindings.find((binding) => managedValue(selectedStand.id, binding.id) === targetValue);
  const selectedEnv = legacyEnvironments.find((env) => legacyValue(env) === targetValue);
  const validTarget = !!data && (!!selectedBinding || selectedEnv !== undefined);
  useEffect(() => {
    if (!data || target.key === targetKey) return;
    const stand = code === requestedProject && requestedStand !== null ? data.run_stands?.find((item) => item.id === requestedStand) : undefined;
    const binding = stand?.bindings.find((item) => item.id === "baseUrl") ?? stand?.bindings[0];
    setTarget({ key: targetKey, value: stand && binding ? managedValue(stand.id, binding.id) : "",
      ...(code === requestedProject && requestedStand !== null && !binding ? { message: "Стенд из ссылки недоступен для запуска или не содержит сервисов. Выберите окружение или стенд вручную." } : {}) });
  }, [data, target.key, targetKey, code, requestedProject, requestedStand]);
  const [form, setForm] = useState({ ref: "", tags: "", groups: "", destructive: false });
  const [branches, setBranches] = useState<{ code: string; data?: string[]; error?: string }>({ code: "" });
  useEffect(() => {
    let alive = true;
    setBranches({ code });
    setForm((previous) => ({ ...previous, ref: "" }));
    if (code) {
      getJson<string[]>(`/api/projects/${encodeURIComponent(code)}/catalog/branches`)
        .then((data) => {
          if (!alive) return;
          setBranches({ code, data });
          setForm((previous) => ({ ...previous, ref: data.includes("main") ? "main" : "" }));
        })
        .catch((e: Error) => { if (alive) setBranches({ code, error: e.message }); });
    }
    return () => { alive = false; };
  }, [code]);
  const currentBranches = branches.code === code ? branches.data : undefined;
  const branchesError = branches.code === code ? branches.error : undefined;
  const [suites, setSuites] = useState<string[]>([]);
  const [mode, setMode] = useState<"all" | "exact" | "filter">("all");
  const [filterDraft, setFilterDraft] = useState("");
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [search, setSearch] = useState("");
  useEffect(() => {
    setSuites([]);
    setSelected({});
    setSearch("");
    setForm((previous) => ({ ...previous, destructive: false }));
  }, [code]);
  const [testCatalog, setTestCatalog] = useState<{ key: string; data?: Record<string, TestCatalog>; error?: string }>({ key: "" });
  const catalogKey = JSON.stringify([code, form.ref, suites]);
  useEffect(() => {
    let alive = true;
    setTestCatalog({ key: catalogKey });
    if (mode === "exact" && code && suites.length && form.ref.trim()) {
      Promise.all(suites.map(async (suite) => [suite, await getJson<TestCatalog>(`/api/projects/${encodeURIComponent(code)}/catalog/test-ids?suite=${encodeURIComponent(suite)}&ref=${encodeURIComponent(form.ref)}`)] as const))
        .then((entries) => { if (alive) setTestCatalog({ key: catalogKey, data: Object.fromEntries(entries) }); })
        .catch((e: Error) => { if (alive) setTestCatalog({ key: catalogKey, error: e.message }); });
    }
    return () => { alive = false; };
  }, [catalogKey, mode]);
  const currentCatalog = testCatalog.key === catalogKey ? testCatalog.data : undefined;
  const catalogError = testCatalog.key === catalogKey ? testCatalog.error : undefined;
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();
  const canDestroy = role === "admin" || role === "tester";
  const destructive = suites.some((name) => data?.suites.find((s) => s.path === name)?.destructive || currentCatalog?.[name]?.destructive);
  const blocked = !validTarget || !suites.length || suites.length > 20 || !currentBranches?.includes(form.ref) || submitting ||
    (destructive && (!canDestroy || !form.destructive)) ||
    (mode === "exact" && (!currentCatalog || !!catalogError || suites.some((name) => !selected[name]?.length || selected[name].length > 500)));
  const words = (value: string) => value.split(/[\s,]+/).filter(Boolean);
  const toggleSuite = (name: string, checked: boolean) => {
    setSuites((previous) => checked ? [...previous, name] : previous.filter((s) => s !== name));
    if (!checked) setSelected((previous) => { const next = { ...previous }; delete next[name]; return next; });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (blocked) return;
    setError(null); setSubmitting(true);
    const entries = suites.map((suite) => ({ suite, ...(mode === "exact" ? { test_ids: selected[suite] } : mode === "filter" ? { tests: filterDraft.split(/\r?\n/).map((x) => x.trim()).filter(Boolean) } : {}) }));
    const common = { project: code, env: selectedBinding?.id ?? selectedEnv!, ...(selectedStand ? { stand_id: selectedStand.id } : {}), ref: form.ref, tags: words(form.tags), groups: words(form.groups), destructive: form.destructive };
    const request = entries.length === 1
      ? sendJson<Job>("/api/jobs", "POST", { ...common, ...entries[0] }).then((job) => navigate(`/jobs/${job.id}`))
      : sendJson<JobBatch>("/api/job-batches", "POST", { ...common, suites: entries }).then((batch) => navigate(`/batches/${batch.id}`));
    request.catch((e: Error) => setError(e.message)).finally(() => setSubmitting(false));
  };
  return <>
    <header className="page-head"><h1>Запуск прогона</h1></header>
    {(projects.data?.length ?? 0) > 0 && <label>Проект<select value={code} onChange={(e) => { const nextCode = e.target.value; setProject({ query, code: nextCode }); setTarget({ key: JSON.stringify([nextCode, query]), value: "" }); setSuites([]); setSelected({}); setSearch(""); setForm({ ...form, ref: "", destructive: false }); }}>{!projects.data?.some((p) => p.code === code) && <option value={code}>Проект из ссылки недоступен</option>}{projects.data?.map((p) => <option key={p.code} value={p.code}>{p.title}</option>)}</select></label>}
    <Loaded state={catalog}>{({ data }) => !data ? <p className="message">Проектов нет</p> : <form className="card form" onSubmit={submit}>
      <fieldset className="test-picker"><legend>Наборы (выбрано {suites.length} из 20)</legend>
        <p className="sub">Каждый набор запускается отдельной задачей с собственным отчётом. Общий тест в двух наборах выполнится дважды.</p>
        <SuiteTree key={code} suites={data.suites.filter((s) => canDestroy || !s.destructive)} selected={suites} toggleSuite={toggleSuite} />
      </fieldset>
      <label>Окружение или стенд<select required value={validTarget ? targetValue : ""} onChange={(e) => setTarget({ key: targetKey, value: e.target.value })}>
        <option value="">Выберите окружение или сервис стенда</option>
        {legacyEnvironments.length > 0 && <optgroup label="Окружения">{legacyEnvironments.map((env) => <option key={env} value={legacyValue(env)}>{env}</option>)}</optgroup>}
        {runStands.map((stand) => <optgroup key={stand.id} label={`${stand.name}${stand.status === "limited" ? " · работает с ограничениями" : ""}`}>
          {stand.bindings.map((binding) => <option key={binding.id} value={managedValue(stand.id, binding.id)}>{stand.name} · {binding.title || binding.id}{stand.status === "limited" ? " · работает с ограничениями" : ""}</option>)}
        </optgroup>)}
      </select></label>
      {target.key === targetKey && target.message && <p className="message">{target.message}</p>}
      {targetValue && !validTarget && <p className="message">Выбранное окружение или сервис больше недоступны. Сделайте выбор вручную.</p>}
      <label>Ветка тестов<select required value={form.ref} disabled={!currentBranches?.length} onChange={(e) => { setForm({ ...form, ref: e.target.value }); setSelected({}); setSearch(""); }}><option value="">{branchesError ? "Ветки недоступны" : !currentBranches ? "Загрузка веток…" : !currentBranches.length ? "Ветки не найдены" : "Выберите ветку"}</option>{currentBranches?.map((branch) => <option key={branch} value={branch}>{branch}</option>)}</select></label>
      {branchesError && <p className="message error">Не удалось загрузить ветки: {branchesError}</p>}
      <label>Метки, через пробел (необязательно)<input value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} /></label>
      <label>Группы безопасности sg-..., через пробел (необязательно)<input value={form.groups} onChange={(e) => setForm({ ...form, groups: e.target.value })} /></label>
      <fieldset className="test-picker"><legend>Какие тесты запустить</legend>
        <label className="check"><input type="radio" name="test-mode" checked={mode === "all"} onChange={() => setMode("all")} /> Весь набор</label>
        <label className="check"><input type="radio" name="test-mode" checked={mode === "exact"} onChange={() => setMode("exact")} /> Выбрать существующие тесты</label>
        <label className="check"><input type="radio" name="test-mode" checked={mode === "filter"} onChange={() => setMode("filter")} /> Фильтр по названию</label>
        {mode === "filter" && <label>Названия или их части, по одному на строку<textarea rows={3} value={filterDraft} onChange={(e) => setFilterDraft(e.target.value)} /><span className="sub">Совпадение по подстроке без учёта регистра. Пустой фильтр запускает весь набор.</span></label>}
        {mode === "exact" && suites.length > 0 && (!form.ref.trim() ? <p className="sub">Укажите ветку.</p> : catalogError ? <p className="message error">Не удалось загрузить тесты: {catalogError}</p> : !currentCatalog ? <p className="sub">Загрузка списков тестов…</p> : <>
          <p className="sub">Выберите хотя бы один тест в каждом наборе, не более 500 на набор.</p><label>Поиск по списку<input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Название или путь теста" /></label>
          {suites.map((name) => { const c = currentCatalog[name]; const ids = selected[name] ?? []; const visible = c.items.filter((item) => `${item.label} ${item.id}`.toLowerCase().includes(search.toLowerCase()));
            return <details className="test-selection" key={name} open><summary>{name}</summary><p className="sub">Ветка: {c.ref}{c.commit ? ` · ${c.commit.slice(0, 12)}` : ""}. {c.gate && "Gate запустится дополнительно. "}{c.runner === "pytest" && "Выбор теста pytest включает все варианты его параметров."}</p>
              {c.destructive && <p className="message">Набор содержит разрушающие тесты. {canDestroy ? "Для запуска отметьте разрешение ниже." : "Запуск доступен тестировщику или администратору."}</p>}
              <div className="selection-actions"><span>Выбрано: {ids.length} из {c.items.length} (не более 500){!ids.length && " · выберите хотя бы один тест"}</span><button type="button" className="secondary" disabled={!visible.length} onClick={() => setSelected((previous) => ({ ...previous, [name]: [...new Set([...(previous[name] ?? []), ...visible.map((item) => item.id)])].slice(0, 500) }))}>Выбрать видимые</button><button type="button" className="secondary" disabled={!ids.length} onClick={() => setSelected((previous) => ({ ...previous, [name]: [] }))}>Очистить</button></div><TestTree items={c.items} visible={visible} runner={c.runner} selected={ids} setSelected={(value) => setSelected((previous) => ({ ...previous, [name]: value }))} searching={search.length > 0} />
            </details>; })}
        </>)}
      </fieldset>
      {canDestroy && <label className="check"><input type="checkbox" checked={form.destructive} onChange={(e) => setForm({ ...form, destructive: e.target.checked })} /> Разрешить разрушающие тесты: они могут заблокировать учётки на стенде</label>}
      {error && <p className="message error">{error}</p>}<button className="primary" disabled={blocked}>Запустить</button>
    </form>}</Loaded>
  </>;
}

export function BatchPage() {
  const { id = "" } = useParams();
  const state = usePolling<JobBatch>(`/api/job-batches/${encodeURIComponent(id)}`, (batch) => !active(batch.status));
  return <Loaded state={state}>{(batch) => <>
    <header className="page-head"><div><h1>Пакет прогонов</h1><div className="sub">{batch.id} · {batch.project} · {batch.created_by} · {formatDate(batch.created)}</div></div></header>
    <p className="message">Задачи завершены: {batch.finished} из {batch.total}. В очереди: {batch.counts.queued}, выполняются: {batch.counts.running}, завершены: {batch.counts.done}, сбой: {batch.counts.failed}, отменены: {batch.counts.cancelled}.</p>
    <progress value={batch.finished} max={batch.total} aria-label="Завершено задач" />
    <div className="table-wrap"><table><thead><tr><th>Задача</th><th>Состояние</th><th>Прогон</th></tr></thead><tbody>{batch.jobs.map((job) => <tr key={job.id}><td><Link to={`/jobs/${job.id}`}>{job.title}</Link></td><td><Status value={job.status} /></td><td>{job.run_id ? <Link to={`/runs/${job.run_id}`}>открыть отчёт</Link> : "-"}</td></tr>)}</tbody></table></div>
  </>}</Loaded>;
}

export function JobsPage({ canRun }: { canRun: boolean }) {
  const state = usePolling<{ total: number; items: Job[] }>("/api/jobs", (page) => !page.items.some((j) => active(j.status)));
  return (
    <>
      <header className="page-head">
        <h1>Задачи</h1>
        {canRun && <Link className="button" to="/start">Запустить прогон</Link>}
      </header>
      <Loaded state={state}>
        {(page) =>
          page.items.length === 0 ? <p className="message">Задач пока нет</p> : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>№</th><th>Задача</th><th>Состояние</th><th>Длительность</th><th>Создана</th><th>Кто</th><th>Прогон</th></tr></thead>
                <tbody>
                  {page.items.map((job) => (
                    <tr key={job.id}>
                      <td><Link to={`/jobs/${job.id}`}>{job.id}</Link></td>
                      <td className="wide">{job.title}{job.batch_id && <> · <Link to={`/batches/${job.batch_id}`}>пакет</Link></>}</td>
                      <td className="nowrap"><Status value={job.status} /></td>
                      <td className="nowrap"><JobDuration job={job} /></td>
                      <td className="nowrap">{formatDate(job.created)}</td>
                      <td>{job.created_by}</td>
                      <td>{job.run_id ? <Link to={`/runs/${job.run_id}`}>открыть</Link> : "-"}</td>
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

export function JobPage({ canRun }: { canRun: boolean }) {
  const { id = "" } = useParams();
  const state = usePolling<JobDetail>(`/api/jobs/${id}`, (job) => !active(job.status));
  const [error, setError] = useState<string | null>(null);
  const [asking, setAsking] = useState(false);
  const cancel = () =>
    sendJson(`/api/jobs/${id}/cancel`, "POST").then(() => setAsking(false)).catch((e: Error) => setError(e.message));
  return (
    <Loaded state={state}>
      {(job) => (
        <>
          <header className="page-head">
            <div>
              <h1>{job.title}</h1>
              <div className="sub">Задача {job.id}, запустил {job.created_by}{job.agent ? `, агент ${job.agent}` : ""}</div>
            </div>
            <Status value={job.status} />
            {canRun && active(job.status) && (
              <button className="link" disabled={job.cancel_requested} onClick={() => setAsking(true)}>
                {job.cancel_requested ? "Отменяется…" : "Отменить"}
              </button>
            )}
          </header>
          {error && <p className="message error">{error}</p>}
          {asking && (
            <ConfirmDialog
              title={`Отменить задачу ${job.id}`}
              info={{
                what: [job.status === "queued" ? "Убрать задачу из очереди" : "Остановить выполнение задачи"],
                risks: job.kind === "stand-build"
                  ? [job.status === "queued"
                    ? "Экземпляр стенда и резервации портов сохранятся"
                    : "Текущая стадия прервётся; контейнеры, данные и предыдущие отчёты сохранятся. Состояние стенда может потребовать проверки"]
                  : job.kind === "stand"
                  ? ["Действие со стендом прервётся на середине: стенд может остаться в промежуточном состоянии, понадобится перезапуск или пересборка"]
                  : ["Прогон прервётся, результатов не будет", "Начатые на стенде изменения тестов не откатываются"],
              }}
              confirmLabel="Отменить задачу"
              danger
              onClose={() => setAsking(false)}
              onConfirm={cancel}
            />
          )}
          {job.batch_id && <p className="links">Пакет: <Link to={`/batches/${job.batch_id}`}>{job.batch_id}</Link></p>}
          {job.message && <p className="message">{job.message}</p>}
          {job.run_id && <p className="links">Прогон: <Link to={`/runs/${job.run_id}`}>{job.run_id}</Link></p>}
          {job.status === "queued" && <p className="message">Ждёт свободного агента и стенд</p>}
          <pre className="log">{job.log || "Лог пока пуст"}</pre>
        </>
      )}
    </Loaded>
  );
}
