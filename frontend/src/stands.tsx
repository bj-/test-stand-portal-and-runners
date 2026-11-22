import { useEffect, useRef, useState, type ReactNode, type SyntheticEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getJson, sendJson, type Me, type Project, type Stand, type StandAction, type StandInfo, type StandRun } from "./api";
import { Loaded, Markdown, formatDate, useLoad } from "./common";
import { ConfirmDialog, type Consequences } from "./confirm";
import { TestDatasets } from "./test-data";

const JOB_STATUS: Record<string, { icon: string; tone: string }> = {
  queued: { icon: "○", tone: "muted" },
  running: { icon: "▶", tone: "warning" },
  done: { icon: "✓", tone: "good" },
  failed: { icon: "✕", tone: "critical" },
  cancelled: { icon: "–", tone: "muted" },
};

const HEALTH: Record<string, { icon: string; label: string; tone: string }> = {
  ok: { icon: "✓", label: "работает", tone: "good" },
  limited: { icon: "⚠", label: "Работает с ограничениями", tone: "warning" },
  broken: { icon: "✕", label: "не работает", tone: "critical" },
  unknown: { icon: "?", label: "не проверен", tone: "muted" },
};

// В хранилище попадает только раскрытие, без содержимого карточек и доступов.
function useDisclosure(storageScope: string, identity: string[], initiallyOpen: boolean) {
  const key = `portal:stands:disclosure:v1:${JSON.stringify([storageScope, ...identity])}`;
  const [open, setOpen] = useState(() => {
    try {
      const saved = localStorage.getItem(key);
      return saved === "true" ? true : saved === "false" ? false : initiallyOpen;
    } catch {
      return initiallyOpen;
    }
  });
  const update = (value: boolean) => {
    setOpen(value);
    try { localStorage.setItem(key, String(value)); } catch { /* Без storage раскрытие работает в текущей странице. */ }
  };
  return { open, onToggle: (event: SyntheticEvent<HTMLDetailsElement>) => {
    if (event.target === event.currentTarget) update(event.currentTarget.open);
  } };
}

// При обновлении оставляем карточки на месте, сохраняя раскрытие и открытые диалоги.
function useRefresh<T>(url: string, reload: number) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  useEffect(() => {
    let active = true;
    setState((previous) => ({ ...previous, error: null, loading: true }));
    getJson<T>(url)
      .then((data) => { if (active) setState({ data, error: null, loading: false }); })
      .catch((error: Error) => { if (active) setState((previous) => ({ ...previous, error: error.message, loading: false })); });
    return () => { active = false; };
  }, [url, reload]);
  return state;
}

export function StandsPage({ storageScope, me }: { storageScope: string; me: Me }) {
  const [reload, setReload] = useState(0);
  const projects = useRefresh<Project[]>("/api/projects", reload);
  const canCreate = !me.is_guest && ["admin", "tester", "developer"].includes(me.role);
  return (
    <>
      <header className="page-head">
        <h1>Стенды</h1>
        {canCreate && <Link to="/stands/new">Создать стенд</Link>}
        {!me.is_guest && me.role === "admin" && <Link to="/stands/projects/new">Создать проект</Link>}
        <button className="link" onClick={() => setReload((value) => value + 1)}>Обновить</button>
      </header>
      {projects.error && <p className="message error" role="alert">Не удалось загрузить проекты: {projects.error}</p>}
      {projects.loading && <p className="message" role="status">{projects.data ? "Обновление проектов…" : "Загрузка проектов…"}</p>}
      {projects.data?.length === 0 && <p className="message">Проекты в установке не описаны</p>}
      <div className="stand-projects">
        {projects.data?.map((project) => <ProjectStands key={project.code} project={project} reload={reload} storageScope={storageScope} me={me} />)}
      </div>
    </>
  );
}

function ProjectStands({ project, reload, storageScope, me }: { project: Project; reload: number; storageScope: string; me: Me }) {
  const stands = useRefresh<Stand[]>(`/api/projects/${encodeURIComponent(project.code)}/stands`, reload);
  const disclosure = useDisclosure(storageScope, ["project", project.code], true);
  return (
    <details className="stand-project" {...disclosure}>
      <summary className="project-head">
        <h2>{project.title}</h2>
        {stands.data && <span className="sub">Стендов: {stands.data.length}</span>}
        {stands.loading && <span className="sub">{stands.data ? "Обновление…" : "Загрузка…"}</span>}
        {stands.error && <span className="tag critical">Ошибка загрузки</span>}
      </summary>
      <div className="project-stands-body" aria-busy={stands.loading}>
        {!me.is_guest && ["admin", "tester", "developer"].includes(me.role) && <p className="links">
          <Link to={`/stands/new?project=${encodeURIComponent(project.code)}`}>Создать стенд в проекте</Link>
          {me.role === "admin" && <Link to={`/stands/catalog/${encodeURIComponent(project.code)}`}>Каталог проекта</Link>}
        </p>}
        {stands.error && <p className="message error" role="alert">Не удалось загрузить стенды: {stands.error}</p>}
        {stands.loading && !stands.data && <p className="message" role="status">Загрузка стендов…</p>}
        {stands.data?.length === 0 && <p className="message">Стенды в проекте не описаны</p>}
        <div className="stands">
          {stands.data?.map((stand) => <StandCard key={stand.name} project={project.code} stand={stand} storageScope={storageScope} me={me} />)}
        </div>
      </div>
    </details>
  );
}

function StandCard({ project, stand, storageScope, me }: { project: string; stand: Stand; storageScope: string; me: Me }) {
  const disclosure = useDisclosure(storageScope, ["stand", project, stand.name], false);
  const health = HEALTH[stand.health.state] ?? HEALTH.unknown;
  const limited = stand.health.state === "limited";
  const broken = stand.health.state === "broken";
  const reasons = stand.health.reasons.filter((reason) => reason !== "известная проблема");
  return (
    <details className={`card stand ${limited || broken ? stand.health.state : ""}`} {...disclosure}>
      <summary className="stand-head">
        <h3>{stand.title}</h3>
        {stand.mode === "ephemeral" && <span className="tag info">Временный{stand.expires && <> · до <time dateTime={stand.expires}>{formatDate(stand.expires)}</time></>}</span>}
        <span className={`tag ${health.tone}`}>{health.icon} {health.label}</span>
        {stand.busy && <span className="tag warning">▶ занят</span>}
      </summary>
      <div className="stand-body">
        {stand.source === "database" && !me.is_guest && <p className="links">
          <Link to={`/stands/${encodeURIComponent(project)}/${encodeURIComponent(stand.name)}`}>Сборки и состояние</Link>
          {stand.can_run && <Link to={`/start?project=${encodeURIComponent(project)}&stand=${encodeURIComponent(stand.name)}`}>Запустить прогон</Link>}
        </p>}

        {(limited || broken) && (
          <div className={`alert ${limited ? "stand-health-limited" : ""}`}>
            {broken && <strong>Стенд не готов к работе</strong>}
            {stand.problem && <Markdown>{stand.problem}</Markdown>}
            {reasons.length > 0 && (
              <ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
            )}
          </div>
        )}
        {stand.health.state === "unknown" && (
          <p className="sub">Нет проверки или прогона после последнего развёртывания.</p>
        )}

        {stand.description && <Markdown>{stand.description}</Markdown>}

        {stand.links.length > 0 && (
          <Block title="Адреса">
            <table className="kv">
              <tbody>
                {stand.links.filter((link) => !me.is_guest || !link.url.startsWith("/api/stand-builds/")).map((link) => (
                  <tr key={link.url}>
                    <th>{link.title}</th>
                    <td><a href={link.url} target="_blank" rel="noreferrer">{link.url}</a></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Block>
        )}

        {stand.access && stand.access.length > 0 && (
          <Block title="Доступы">
            <table className="kv">
              <tbody>{stand.access.map((a) => <AccessRow key={a.title} {...a} />)}</tbody>
            </table>
          </Block>
        )}

        {stand.info && <BuildInfo info={stand.info} />}

        <Block title="Окружения тестов и последние прогоны">
          <ul className="plain">
            {stand.environments.map((env) => (
              <li key={env}>
                <code>{env}</code>
                <RunLine run={stand.runs.find((r) => r.env === env)} />
              </li>
            ))}
          </ul>
        </Block>

        {stand.health.checks.length > 0 && (
          <details>
            <summary className="sub">Проверка стенда{stand.health.checked ? `, ${formatDate(stand.health.checked)}` : ""}</summary>
            <ul className="plain checks">
              {stand.health.checks.map((c) => (
                <li key={c.name}>
                  <span className={`tag ${c.ok ? "good" : "critical"}`}>{c.ok ? "✓" : "✕"}</span> {c.name}
                  {c.detail && <span className="sub"> - {c.detail}</span>}
                </li>
              ))}
            </ul>
          </details>
        )}

        <StandActions project={project} stand={stand} />

        {stand.jobs.length > 0 && (
          <Block title="Последние действия">
            <ul className="plain">
              {stand.jobs.map((job) => {
                const s = JOB_STATUS[job.status] ?? { icon: "?", tone: "muted" };
                return (
                  <li key={job.id}>
                    <span className={`tag ${s.tone}`}>{s.icon}</span>{" "}
                    <Link to={`/jobs/${job.id}`}>{job.title}</Link>{" "}
                    <span className="sub">{job.created_by}, {formatDate(job.created)}</span>
                  </li>
                );
              })}
            </ul>
          </Block>
        )}
      </div>
    </details>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="block">
      <h3>{title}</h3>
      {children}
    </div>
  );
}

function AccessRow({ title, value, secret }: { title: string; value: string; secret: boolean }) {
  const [shown, setShown] = useState(!secret);
  const [copied, setCopied] = useState(false);
  const copy = () =>
    navigator.clipboard?.writeText(value).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  return (
    <tr>
      <th>{title}</th>
      <td>
        <code>{shown ? value : "••••••••"}</code>{" "}
        {secret && <button className="link" onClick={() => setShown(!shown)}>{shown ? "скрыть" : "показать"}</button>}
        <button className="link" onClick={copy}>{copied ? "скопировано" : "копировать"}</button>
      </td>
    </tr>
  );
}

function BuildInfo({ info }: { info: StandInfo }) {
  const components = info.components ?? [];
  return (
    <Block title="Что на стенде">
      <table className="kv">
        <tbody>
          <tr><th>Собран</th><td>{info.built ? formatDate(info.built) : "нет сведений"}{info.built_on === "server" ? ", на сервере" : ""}</td></tr>
          <tr><th>Развёрнут</th><td>{info.deployed ? formatDate(info.deployed) : "нет сведений"}</td></tr>
          {info.migrations?.last && (
            <tr>
              <th>Миграции</th>
              <td>
                применено {info.migrations.applied}
                {info.migrations.in_build !== undefined ? ` из ${info.migrations.in_build}` : ""}, последняя <code>{info.migrations.last}</code>
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {components.length > 0 ? (
        <div className="table-wrap">
          <table className="components">
            <thead>
              <tr><th>Компонент</th><th>Ветка</th><th>Коммит</th><th>Срез (время коммита)</th><th>Версия</th><th>API</th></tr>
            </thead>
            <tbody>
              {components.map((c) => (
                <tr key={c.name}>
                  <td>{c.name}</td>
                  <td>{c.ref ?? "-"}</td>
                  <td><code title={c.subject}>{c.commit ?? "-"}</code></td>
                  <td className="nowrap">{c.commit_date ? formatDate(c.commit_date) : "-"}</td>
                  <td>{c.version || "-"}</td>
                  <td>{c.api_version || "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="sub">Ревизии и версии появятся после пересборки из портала.</p>
      )}
    </Block>
  );
}

function RunLine({ run }: { run: StandRun | undefined }) {
  if (!run) return <span className="sub"> - прогонов не было</span>;
  const bad = run.gate_failed || run.failed + run.errors > 0;
  const tone = run.gate_failed ? "critical" : bad ? "warning" : "good";
  return (
    <span className="run-line">
      {" "}
      <span className={`tag ${tone}`}>
        {run.gate_failed ? "✕ gate не пройден" : `${run.passed} из ${run.tests}`}
      </span>{" "}
      <Link to={`/runs/${run.id}`}>{run.suite}</Link> <span className="sub">{formatDate(run.started)}</span>
    </span>
  );
}

// Что делает каждое действие и чем грозит - показывается в окне подтверждения. Общее для любых стендов: устройство
// конкретного стенда описано в его манифесте (описание, засев).
const ACTION_INFO: Record<string, Consequences> = {
  deploy: {
    what: [
      "Собрать образы компонентов стенда из указанных веток или коммитов",
      "Накатить миграции базы и перезапустить приложения",
      "Применить включённые наборы с автоприменением; ошибка набора останавливает цепочку",
      "Обновить сведения о стенде и запустить смоук-прогон",
    ],
    risks: [
      "Стенд недоступен на время сборки и перезапуска (обычно несколько минут)",
      "Если на стенде идёт прогон, пересборка дождётся его окончания, а новые прогоны будут ждать пересборку",
      "Код на стенде сменится на выбранные ревизии; неудачная ревизия сделает стенд нерабочим до следующей пересборки",
      "Данные стенда сохраняются, но миграции из выбранной ревизии изменят схему базы",
    ],
  },
  restart: {
    what: [
      "Поднять все сервисы стенда без пересборки, пересоздать изменённые контейнеры",
      "Обновить проверку стенда и запустить смоук-прогон",
    ],
    risks: [
      "Сервисы стенда кратковременно недоступны, идущие запросы могут оборваться",
      "Если на стенде идёт прогон, перезапуск дождётся его окончания",
      "Данные стенда сохраняются",
    ],
  },
  reset: {
    what: [
      "Удалить ВСЕ данные стенда: базу, файлы, кэш и почту",
      "Развернуть стенд заново, накатить миграции",
      "Засеять тестовые данные, если засев описан у стенда, и запустить смоук-прогон",
      "До смоук-прогона применить включённые наборы с автоприменением; ошибка набора останавливает цепочку",
    ],
    risks: [
      "Необратимо: всё, что создано на стенде вручную и тестами, пропадёт без возможности восстановления",
      "Коллеги, работающие со стендом, потеряют свои данные и сеансы",
      "Стенд недоступен несколько минут",
    ],
  },
};

const MANAGED_ACTION_INFO: Record<string, Consequences> = {
  deploy: {
    what: ["Повторно собрать и развернуть последние сохранённые версии исходников (SHA).",
      "Выполнить этапы шаблона, сохранённый план тестовых данных и проверки стенда. Результат появится в истории сборок."],
    risks: ["Стенд будет недоступен во время сборки и развёртывания.",
      "Данные сохраняются; миграции и сохранённый план подготовки данных могут изменить их по правилам шаблона."],
  },
  restart: {
    what: ["Перезапустить сервисы существующего стенда без пересборки и применения тестовых данных.",
      "Результат появится в журнале задачи."],
    risks: ["Сервисы кратковременно недоступны. Перезапуск дождётся освобождения стенда.", "Данные и тома стенда сохраняются."],
  },
};

export function StandActions({ project, stand, disabled = false, onQueued }: {
  project: string; stand: Stand; disabled?: boolean; onQueued?: () => void;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState<StandAction | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const [testDataOpen, setTestDataOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const [queued, setQueued] = useState<{ id: number; message: string } | null>(null);

  const allowed = stand.actions.filter((a) => a.allowed && a.name !== "seed" && a.name !== "reset");
  if (allowed.length === 0 && !stand.can_view_test_data) return null;

  const queue = async (action: StandAction, body: object) => {
    if (pendingRef.current || disabled) return;
    pendingRef.current = true;
    setPending(true);
    setError(null);
    setQueued(null);
    try {
      const job = await sendJson<{ id: number; message?: string }>(`/api/projects/${encodeURIComponent(project)}/stands/${encodeURIComponent(stand.name)}/${action.name}`, "POST", body);
      if (stand.source === "database") {
        setQueued({ id: job.id, message: job.message ?? "Действие поставлено в очередь." });
        setOpen(null);
        onQueued?.();
      } else navigate(`/jobs/${job.id}`);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "Не удалось получить результат запроса. Обновите состояние стенда.";
      setError(message);
      throw cause;
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };
  const start = (action: StandAction) => {
    if (!ACTION_INFO[action.name]) {
      void queue(action, {}).catch(() => undefined); // «Состояние», «Логи» - подтверждение не нужно
      return;
    }
    setParams(Object.fromEntries(stand.params.map((p) => [p.name, p.default])));
    setOpen(action);
  };

  return (
    <>
      <div className="stand-actions" role="group" aria-label={`Действия стенда ${stand.title}`}>
        <div className="actions stand-actions-main">
          {allowed.filter((action) => !["status", "logs"].includes(action.name)).map((action) => (
            <button key={action.name} className={action.name === "deploy" ? "primary" : "secondary"}
              disabled={disabled || pending} onClick={() => start(action)}>{action.title}</button>
          ))}
          {stand.can_view_test_data && <button className="secondary" onClick={() => setTestDataOpen(true)}>Тестовые данные</button>}
        </div>
        <div className="actions stand-actions-tools">
          {allowed.filter((action) => ["status", "logs"].includes(action.name)).map((action) => (
            <button key={action.name} className="secondary" disabled={disabled || pending} onClick={() => start(action)}>
              <span aria-hidden="true">{action.name === "status" ? "↻" : "≡"}</span> {action.title}
            </button>
          ))}
        </div>
      </div>
      {error && !open && <p className="message error" role="alert">{error}</p>}
      {queued && <p className="notice" role="status">{queued.message} <Link to={`/jobs/${queued.id}`}>Журнал задачи №{queued.id} →</Link></p>}
      {testDataOpen && <TestDataDialog project={project} stand={stand} onClose={() => setTestDataOpen(false)} />}
      {open && (
        <ConfirmDialog
          title={`${open.title}: ${stand.title}`}
          info={stand.source === "database" ? MANAGED_ACTION_INFO[open.name] : ACTION_INFO[open.name]}
          danger={open.destructive}
          confirmWord={open.destructive ? stand.name : undefined}
          confirmLabel={open.destructive ? "Удалить данные и развернуть" : "Поставить в очередь"}
          onClose={() => setOpen(null)}
          onConfirm={() => queue(open, open.name === "deploy" ? { params } : open.destructive ? { confirm: true } : {})}
        >
          {open.name === "deploy" && stand.params.length > 0 && (
            <div className="form">
              {stand.params.map((p) => (
                <label key={p.name}>{p.title}
                  <input value={params[p.name] ?? ""} placeholder={p.default}
                    onChange={(e) => setParams({ ...params, [p.name]: e.target.value })} />
                </label>
              ))}
              <span className="sub">Ветка или коммит. Пустое поле - ревизия по умолчанию.</span>
            </div>
          )}
        </ConfirmDialog>
      )}
    </>
  );
}

const SEED_INFO: Consequences = {
  what: [
    "Выполнить базовый засев, описанный в конфигурации стенда",
    "Применить включённые наборы с автоприменением в заданном порядке",
  ],
  risks: [
    "Данные изменятся по правилам засева и подключённых наборов",
    "Ошибка применения набора остановит цепочку; изменения предыдущих целей могут сохраниться",
    "Если на стенде идёт прогон, засев дождётся его окончания",
  ],
};

/** Просмотр не создаёт задач. Засев и сброс запускаются только после подтверждения. */
function TestDataDialog({ project, stand, onClose }: { project: string; stand: Stand; onClose: () => void }) {
  const navigate = useNavigate();
  const [action, setAction] = useState<StandAction | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    return () => opener.current?.focus();
  }, []);

  useEffect(() => {
    // У подтверждения собственный обработчик Escape; окно просмотра в этот момент снято.
    if (action || !stand.can_view_test_data) return;
    const element = box.current;
    element?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
      if (event.key !== "Tab" || !element) return;
      const controls = Array.from(element.querySelectorAll<HTMLElement>('a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]')).filter((control) => control.getClientRects().length > 0 && !control.matches(":disabled"));
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === element)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === element)) {
        event.preventDefault(); first?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [action, onClose, stand.can_view_test_data]);

  if (!stand.can_view_test_data) return <p className="message">Недостаточно прав для просмотра тестовых данных.</p>;
  const confirmation = action ? (
    <ConfirmDialog
      title={`${action.title}: ${stand.title}`}
      info={action.name === "seed" ? SEED_INFO : ACTION_INFO.reset}
      danger={action.destructive}
      confirmWord={action.destructive ? stand.name : undefined}
      confirmLabel={action.destructive ? "Удалить данные и развернуть" : "Засеять"}
      onClose={() => setAction(null)}
      onConfirm={() => sendJson<{ id: number }>(`/api/projects/${project}/stands/${stand.name}/${action.name}`, "POST", action.destructive ? { confirm: true } : {})
        .then((job) => navigate(`/jobs/${job.id}`))}
    />
  ) : null;

  const seed = stand.actions.find((a) => a.name === "seed");
  const reset = stand.actions.find((a) => a.name === "reset");
  return (
    <>
    <div className="modal-backdrop" style={action ? { display: "none" } : undefined} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div className="modal test-data-dialog" role="dialog" aria-modal="true" aria-labelledby="test-data-title" tabIndex={-1} ref={box}>
        <header><h2 id="test-data-title">Тестовые данные: {stand.title}</h2></header>
        <div className="modal-body">
          <p><Link to="/help#test-datasets" onClick={onClose}>Как создавать наборы</Link></p>
          <section>
            <h3>Состав тестовых данных</h3>
            {stand.seed_info ? <Markdown>{stand.seed_info}</Markdown> : <p className="sub">Состав тестовых данных для этого стенда не описан в конфигурации установки.</p>}
          </section>
          <p className="sub">Открытие окна не меняет данные стенда и не запускает задачи.</p>
          <TestDatasets key={`${project}/${stand.name}`} project={project} stand={stand.name} />
          {(seed?.allowed || reset?.allowed) && <section className="test-data-actions">
            <h3>Управление данными</h3>
            {seed?.allowed && <div>
              <button className="primary" onClick={() => setAction(seed)}>Засеять</button>
              <p className="sub">Выполнить засев стенда и применить включённые наборы с автоприменением. Перед запуском — подтверждение.</p>
            </div>}
            {reset?.allowed && <div>
              <button className="secondary danger" onClick={() => setAction(reset)}>Сбросить данные</button>
              <p className="sub">Удалить ВСЕ данные стенда и развернуть заново. Потребуется ввести название стенда.</p>
            </div>}
          </section>}
          {!seed && <p className="sub">У этого стенда команда засева не описана.</p>}
        </div>
        <footer><button className="secondary" onClick={onClose}>Закрыть</button></footer>
      </div>
    </div>
    {confirmation}
    </>
  );
}

/** Совместимость со ссылками на прежнюю страницу засева. */
export function SeedPage() {
  const { project = "", name = "" } = useParams();
  const navigate = useNavigate();
  const state = useLoad(() => getJson<Stand[]>(`/api/projects/${project}/stands`), [project]);
  return (
    <>
      <p className="links"><Link to="/stands">← Стенды</Link></p>
      <Loaded state={state}>
        {(stands) => {
          const stand = stands.find((s) => s.name === name);
          if (!stand) return <p className="message">Стенд не найден</p>;
          return <TestDataDialog project={project} stand={stand} onClose={() => navigate("/stands")} />;
        }}
      </Loaded>
    </>
  );
}
