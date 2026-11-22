import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, getJson, getStand, sendJson, type Job, type Me, type Stand } from "./api";
import { formatDate } from "./common";
import { bindingTitle, newIdempotencyKey, projectApi, type StandBuild, type StandInstance } from "./stand-api";
import { ConfirmDialog } from "./confirm";
import { StandActions } from "./stands";
import "./stand-detail.css";

const ACTIVE = new Set(["pending", "queued", "running", "creating", "waiting", "building", "deploying", "checking"]);
const ENDED = new Set(["done", "failed", "cancelled"]);
const LABELS: Record<string, [string, string]> = {
  pending: ["Ожидает запуска", "muted"], queued: ["В очереди", "muted"],
  waiting: ["Ожидает исполнителя", "muted"], creating: ["Создаётся", "warning"], created: ["Создан", "muted"],
  running: ["Выполняется", "warning"], building: ["Сборка образов", "warning"],
  deploying: ["Развёртывается", "warning"], checking: ["Проверяется", "warning"],
  ready: ["Готов к работе", "good"], done: ["Завершена", "good"], success: ["Выполнен", "good"],
  limited: ["⚠ Работает с ограничениями", "warning"],
  failed: ["Ошибка", "critical"], cancelled: ["Отменена", "muted"],
  skipped: ["Пропущен", "muted"], idle: ["Не выполнялся", "muted"], stopped: ["Остановлен", "muted"],
  deleting: ["Удаляется", "warning"], deleted: ["Удалён", "muted"],
};
const STAGES = [
  ["build", "Сборка образов"], ["deploy", "Развёртывание"], ["migrate", "Обновление базы"],
  ["seed", "Подготовка данных"], ["healthcheck", "Проверка доступности"], ["smoke", "Проверка работы (smoke)"],
] as const;

function Status({ value }: { value: string }) {
  const [label, tone] = LABELS[value] ?? ["Состояние уточняется", "muted"];
  return <span className={`tag ${tone}`}>{label}</span>;
}

function waitingMessage(value: string | null | undefined) {
  if (!value) return "Ожидаем запуска на выбранном хосте.";
  if (value.includes("подключения исполнителя")) return "Исполнитель на выбранном хосте ещё не подключён. Сборка начнётся после его подключения.";
  if (value.includes("свободного исполнителя")) return "Исполнитель подключён и занят другой задачей. Сборка начнётся, когда он освободится.";
  return value.replaceAll("шаблонов stand-template-v1", "стендов").replaceAll("stand-template-v1", "стендов");
}

function buildNumber(id: string) { return id.replace(/^build-/, ""); }

type Retry = { buildId: string; key: string; phase: "pending" | "uncertain" };
type Snapshot = { instance: StandInstance | null; builds: StandBuild[] | null; stand: Stand | null };
const UNCERTAIN = "Ответ на запрос не получен. Сборка могла начаться. Повторите исходный запрос: портал проверит его результат и не создаст дубликат. До получения ответа запуск другой сборки недоступен.";

function restoredRetry(storageKey: string): Retry | null {
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(storageKey) ?? "null");
    if (saved && typeof saved === "object" && "buildId" in saved && "key" in saved && typeof saved.buildId === "string" && typeof saved.key === "string") {
      return { buildId: saved.buildId, key: saved.key, phase: "uncertain" };
    }
  } catch { /* The page also works when browser storage is unavailable. */ }
  return null;
}

export function StandDetailPage({ me }: { me: Me }) {
  const { project = "", id = "" } = useParams();
  return <StandDetail key={`${project}/${id}`} me={me} project={project} id={id} />;
}

function StandDetail({ me, project, id }: { me: Me; project: string; id: string }) {
  const url = `${projectApi(project)}/stand-instances/${encodeURIComponent(id)}`;
  const storageKey = `stand-retry:${JSON.stringify([me.name, project, id])}`;
  const [data, setData] = useState<Snapshot>({ instance: null, builds: null, stand: null });
  const dataRef = useRef(data);
  dataRef.current = data;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [retry, setRetry] = useState<Retry | null>(() => restoredRetry(storageKey));
  const retryRef = useRef<Retry | null>(retry);
  const [actionError, setActionError] = useState<string | null>(() => retry ? UNCERTAIN : null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const deletePending = useRef(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const mounted = useRef(false);

  function saveRetry(value: Retry | null) {
    retryRef.current = value;
    setRetry(value);
    try {
      if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
    } catch { /* Keep the operation in memory when storage is unavailable. */ }
  }

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    let timer = 0;
    async function tick() {
      setLoading(true);
      const [instanceResult, buildsResult, standResult] = await Promise.allSettled([
        getJson<StandInstance>(url), getJson<StandBuild[]>(`${url}/builds`), getStand(project, id),
      ]);
      if (!alive) return;
      const current = dataRef.current;
      const next = {
        instance: instanceResult.status === "fulfilled" ? instanceResult.value : current.instance,
        builds: buildsResult.status === "fulfilled" ? buildsResult.value : current.builds,
        stand: standResult.status === "fulfilled" ? standResult.value : current.stand,
      };
      dataRef.current = next;
      setData(next);
      const failures: string[] = [];
      if (instanceResult.status === "rejected") {
        const missing = instanceResult.reason instanceof ApiError && instanceResult.reason.status === 404;
        failures.push(missing ? "Стенд не найден или больше недоступен." : "Не удалось обновить состояние стенда.");
      }
      if (buildsResult.status === "rejected") failures.push("Не удалось обновить историю сборок.");
      if (standResult.status === "rejected") failures.push("Не удалось обновить действия стенда.");
      setLoadError(failures.length ? `${failures.join(" ")}${current.instance || current.builds ? " Показаны последние полученные данные." : ""}` : null);
      setLoading(false);
      const active = next.instance && (ACTIVE.has(next.instance.status) || ACTIVE.has(next.instance.job_status ?? "") || ACTIVE.has(next.instance.deletion_status ?? ""));
      const currentBuild = next.builds?.find((build) => build.job_id === next.instance?.job_id);
      const missing = instanceResult.status === "rejected" && instanceResult.reason instanceof ApiError && [403, 404].includes(instanceResult.reason.status);
      if (!missing && (active || next.stand?.jobs.some((job) => ACTIVE.has(job.status)) || (currentBuild && ACTIVE.has(currentBuild.status)) || !next.instance)) {
        timer = window.setTimeout(tick, 2500);
      }
    }
    void tick();
    return () => { alive = false; window.clearTimeout(timer); };
  }, [url, project, id, refresh]);

  const instance = data.instance;
  const removed = !!instance && (instance.status === "deleted" || !!instance.deleted_at);
  const deletionActive = !!instance && (instance.status === "deleting" || ACTIVE.has(instance.deletion_status ?? ""));
  const canManage = !me.is_guest && !!instance && !removed && !deletionActive && !deleting && (["admin", "tester"].includes(me.role) || (me.role === "developer" && me.name === instance.owner_login));
  const canDelete = !me.is_guest && !!instance && !removed && !deletionActive && (instance.can_delete ?? (me.role === "admin" || me.name === instance.owner_login));
  const jobActive = !!instance && (ACTIVE.has(instance.job_status ?? "") || ACTIVE.has(instance.status));

  async function removeStand() {
    if (!canDelete || deletePending.current || jobActive || retryRef.current) return;
    deletePending.current = true;
    setDeleting(true);
    setDeleteError(null);
    try {
      const updated = await sendJson<StandInstance>(url, "DELETE");
      if (!mounted.current) return;
      const next = { ...dataRef.current, instance: updated };
      dataRef.current = next;
      setData(next);
    } catch (error) {
      if (!mounted.current) return;
      setDeleteError(error instanceof ApiError && error.status === 409
        ? "Удаление сейчас недоступно: стенд занят. Дождитесь завершения текущей задачи."
        : error instanceof ApiError && [401, 403].includes(error.status)
        ? "Недостаточно прав для удаления стенда. Обновите страницу и проверьте вход в портал."
        : "Не удалось получить результат запроса на удаление. Обновите состояние: задача могла быть создана. Повторный запрос проверит текущее удаление.");
    } finally {
      deletePending.current = false;
      if (mounted.current) {
        setDeleting(false);
        setConfirmDelete(false);
        setRefresh((value) => value + 1);
      }
    }
  }

  async function repeat(buildId: string, original = false) {
    if (!canManage || retryRef.current?.phase === "pending") return;
    if (retryRef.current && (!original || retryRef.current.buildId !== buildId)) return;
    if (!original && jobActive) return;
    const operation: Retry = { buildId, key: retryRef.current?.key ?? newIdempotencyKey(), phase: "pending" };
    saveRetry(operation);
    setActionError(null);
    setActionNotice(null);
    try {
      const job = await sendJson<Job>(`${url}/retry`, "POST", { build_id: buildId }, { "Idempotency-Key": operation.key });
      if (!mounted.current) return;
      saveRetry(null);
      setActionNotice(ACTIVE.has(job.status)
        ? `Повторная сборка принята. Задача №${job.id} использует те же версии исходников.`
        : `Запрос принят ранее. Задача №${job.id}: ${(LABELS[job.status]?.[0] ?? "состояние уточняется").toLowerCase()}.`);
      // The idempotent response may refer to an already completed job.
      if (dataRef.current.instance) {
        const next: Snapshot = { ...dataRef.current, instance: { ...dataRef.current.instance, job_id: job.id, job_status: job.status,
          status: ACTIVE.has(job.status) ? "waiting" : dataRef.current.instance.status } };
        dataRef.current = next;
        setData(next);
      }
      setRefresh((value) => value + 1);
    } catch (error) {
      if (!mounted.current) return;
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 425, 429].includes(error.status)) {
        saveRetry(null);
        setActionError(error.status === 409
          ? "Сейчас повторить сборку нельзя: стенд уже занят или исходная сборка ещё не завершена. Обновите состояние и попробуйте снова."
          : error.status === 403 ? "Недостаточно прав для повторной сборки."
          : error.status === 401 ? "Войдите снова, чтобы повторить сборку."
          : "Не удалось повторить сборку. Обновите страницу и проверьте доступность стенда и исходной сборки.");
        setRefresh((value) => value + 1);
      } else {
        const uncertain: Retry = { ...operation, phase: "uncertain" };
        saveRetry(uncertain);
        setActionError(UNCERTAIN);
      }
    }
  }

  return <div className="stand-detail">
    <p className="links"><Link to="/stands">← Стенды</Link></p>
    <header className="page-head">
      <h1>{instance?.name ?? "Состояние стенда"}</h1>
      {instance && <Status value={instance.status} />}
      <button className="secondary" onClick={() => setRefresh((value) => value + 1)} disabled={loading}>
        {loading ? "Обновление…" : "Обновить"}
      </button>
      {canDelete && <button className="secondary danger" disabled={deleting || jobActive || retry !== null} onClick={() => setConfirmDelete(true)}>
        {deleting ? "Отправка запроса…" : instance?.deletion_status === "failed" || instance?.deletion_status === "cancelled" ? "Повторить удаление" : "Удалить стенд"}
      </button>}
    </header>
    {loadError && <p className="notice stand-detail-error" role="alert">{loadError}</p>}
    {!instance && !loadError && <p className="message" role="status">Загрузка стенда…</p>}
    {instance && <>
      {instance.status === "limited" && <div className="alert stand-health-limited" role="status">
        Smoke не пройден. Отклик стенда подтверждён.
      </div>}
      {deleteError && <p className="notice stand-detail-error" role="alert">{deleteError}</p>}
      {removed && <p className="notice" role="status">Стенд удалён{instance.deleted_at ? ` ${formatDate(instance.deleted_at)}` : ""}. История сборок сохранена. <Link to="/stands">К списку стендов →</Link></p>}
      {canDelete && jobActive && <p className="sub">Удаление станет доступно после завершения текущей задачи.</p>}
      {instance.deletion_job_id && <section className="card stand-detail-current" aria-labelledby="stand-deletion-job">
        <div className="stand-detail-row"><h2 id="stand-deletion-job">Удаление стенда · задача №{instance.deletion_job_id}</h2><Status value={instance.deletion_status ?? "pending"} /></div>
        {deletionActive && <p role="status">Удаление принято. Ожидаем завершения работы исполнителя; состояние обновляется автоматически.</p>}
        {instance.deletion_status === "failed" && <p className="stand-detail-error" role="alert">Не удалось удалить стенд. Проверьте журнал задачи и повторите удаление.</p>}
        {instance.deletion_status === "cancelled" && <p>Удаление отменено. Для завершения можно повторить удаление.</p>}
        {instance.deletion_message && <p>{instance.deletion_message}</p>}
        <p><Link to={`/jobs/${instance.deletion_job_id}`}>Журнал удаления →</Link></p>
      </section>}
      <dl className="facts stand-detail-facts">
        <div><dt>Проект</dt><dd>{instance.project}</dd></div>
        <div><dt>Владелец</dt><dd>{instance.owner_login}</dd></div>
        <div><dt>Хост</dt><dd>{instance.target_host}</dd></div>
        <div><dt>Профиль ресурсов</dt><dd>{instance.profile}</dd></div>
        <div><dt>Срок работы</dt><dd>{instance.mode === "ephemeral" ? <>Временный стенд{instance.expires ? <> · до <time dateTime={instance.expires}>{formatDate(instance.expires)}</time></> : " · срок не указан"}</> : instance.expires ? <>До <time dateTime={instance.expires}>{formatDate(instance.expires)}</time></> : "Постоянный стенд"}</dd></div>
        {instance.data_bundle && <div><dt>Тестовые данные</dt><dd>{instance.data_bundle.title}<ul>{instance.data_bundle.entries.map((entry) => <li key={entry.module}>{entry.module} · {entry.title} · версия {entry.version}</li>)}</ul></dd></div>}
        <div><dt>Окружения</dt><dd>{instance.environments.join(", ") || "Не указаны"}</dd></div>
        {!removed && <div><dt>Порты на хосте</dt><dd>{Object.entries(instance.ports).length
          ? <ul className="stand-detail-plain-list">{Object.entries(instance.ports).map(([name, port]) => <li key={name}>{bindingTitle(name, instance.output_binding_titles)}: <code>{port}</code></li>)}</ul>
          : "Пока не назначены"}</dd></div>}
        {!removed && <div><dt>Развёрнутая сборка</dt><dd>{instance.deployed_build_id
          ? <a href={`#${instance.deployed_build_id}`}>Сборка {buildNumber(instance.deployed_build_id)}</a>
          : "Пока нет успешного развёртывания"}</dd></div>}
      </dl>
      {instance.job_id && <section className="card stand-detail-current" aria-labelledby="stand-current-job">
        <div className="stand-detail-row"><h2 id="stand-current-job">Текущая задача №{instance.job_id}</h2><Status value={instance.job_status ?? "pending"} /></div>
        {instance.job_status === "queued" && <p>{waitingMessage(instance.waiting_reason)}</p>}
        {instance.job_status === "running" && <p>Исполнитель готовит стенд. Этапы и результаты появятся в истории сборок.</p>}
        {instance.job_status === "cancelled" && <p>Задача отменена. Результаты предыдущих сборок сохранены.</p>}
        {instance.job_status === "failed" && <p>Сборка завершилась ошибкой. Откройте журнал задачи и результаты проверок ниже.</p>}
        <p><Link to={`/jobs/${instance.job_id}`}>{jobActive && canManage ? "Журнал задачи и отмена" : "Журнал задачи"} →</Link></p>
      </section>}
      {actionError && <div className="notice stand-detail-error" role="alert">
        <p>{actionError}</p>
        {retry?.phase === "uncertain" && canManage && <button className="secondary" onClick={() => void repeat(retry.buildId, true)}>Повторить исходный запрос</button>}
      </div>}
      {actionNotice && <p className="notice" role="status">{actionNotice}</p>}
      {!removed && !deletionActive && data.stand && <section className="card" aria-label="Действия стенда">
        <StandActions project={project} stand={data.stand} disabled={deleting || retry !== null}
          onQueued={() => setRefresh((value) => value + 1)} />
        {data.stand.jobs.length > 0 && <ul className="plain">{data.stand.jobs.map((job) => <li key={job.id}>
          <Status value={job.status} /> <Link to={`/jobs/${job.id}`}>{job.title}</Link>
        </li>)}</ul>}
      </section>}
      <section aria-labelledby="stand-build-history">
        <h2 id="stand-build-history">История сборок</h2>
        {data.builds === null && <p className="sub">{loadError ? "История временно недоступна. Нажмите «Обновить»." : "Загрузка истории…"}</p>}
        {data.builds?.length === 0 && <p className="notice">{jobActive
          ? "Сборка появится здесь, когда исполнитель зафиксирует версии исходников. Журнал задачи уже доступен."
          : "Истории сборок пока нет. Подробности доступны в журнале задачи."}</p>}
        <div className="stand-build-list">{data.builds?.map((build) => <BuildCard key={build.id} build={build}
          deployed={!removed && build.id === instance.deployed_build_id} canManage={canManage} removed={removed}
          disabled={jobActive || retry !== null} pending={retry?.phase === "pending" && retry.buildId === build.id}
          onRepeat={() => void repeat(build.id)} />)}</div>
      </section>
      {confirmDelete && <ConfirmDialog title={`Удалить стенд «${instance.name}»?`} danger confirmWord={instance.name} confirmLabel="Удалить стенд"
        info={{ what: ["Исполнитель остановит и удалит контейнеры и данные этого стенда.", "Порты освободятся после успешного удаления. История сборок останется в портале."], risks: ["Сервисы стенда станут недоступны.", "Данные стенда будут удалены без возможности восстановления через портал."] }}
        onConfirm={removeStand} onClose={() => setConfirmDelete(false)} />}
    </>}
  </div>;
}

function BuildCard({ build, deployed, canManage, removed, disabled, pending, onRepeat }: {
  build: StandBuild; deployed: boolean; canManage: boolean; removed: boolean; disabled: boolean; pending: boolean; onRepeat: () => void;
}) {
  const ended = ENDED.has(build.status);
  const liveness = build.stages.find((stage) => stage.name === "liveness");
  const smokeFailed = build.stages.some((stage) => stage.name === "smoke" && stage.status === "failed");
  const limited = build.status === "done" && smokeFailed && liveness?.status === "success";
  const stages: ReadonlyArray<readonly [string, string]> = liveness ? [...STAGES, ["liveness", "Проверка отклика стенда"]] : STAGES;
  const completed = stages.filter(([name]) => build.stages.some((stage) => stage.name === name && stage.status === "success")).length;
  return <article className={`card stand-build${limited ? " stand-build-limited" : ""}`} id={build.id} aria-labelledby={`title-${build.id}`}>
    <header className="stand-detail-row">
      <h3 id={`title-${build.id}`}>Сборка {buildNumber(build.id)}</h3>
      <Status value={limited ? "limited" : build.status} />
      {deployed && <span className="tag good">Развёрнута на стенде</span>}
    </header>
    <p className="sub">Создана {formatDate(build.created)}{build.started ? ` · Начата ${formatDate(build.started)}` : ""}{build.finished ? ` · Завершена ${formatDate(build.finished)}` : ""}</p>
    {smokeFailed && ["success", "failed"].includes(liveness?.status ?? "") && <p className={limited ? "stand-detail-warning" : "stand-detail-error"}>
      Smoke не пройден. {liveness?.status === "success" ? "Отклик стенда подтверждён." : "Отклик стенда не подтверждён."}
    </p>}
    <div className="stand-detail-row stand-build-progress"><span>Этапы: {completed} из {stages.length}</span><progress value={completed} max={stages.length} aria-label={`Выполнено этапов: ${completed} из ${stages.length}`} /></div>
    <ol className="stand-build-stages">{stages.map(([name, title]) => {
      const stage = build.stages.find((item) => item.name === name);
      return <li key={name}><span>{title}</span><Status value={stage?.status ?? (ended ? "idle" : "pending")} /></li>;
    })}</ol>
    <section className="stand-build-sources" aria-label="Версии исходников">
      <h4>Версии исходников</h4>
      {build.components.length === 0 ? <p className="sub">Версии ещё не зафиксированы.</p> : <ul className="stand-detail-plain-list">{build.components.map((component) => <li key={component.id}>
        <div><strong>{component.id}</strong><span className="sub"> Ветка или тег: {component.requested_ref}</span></div>
        <code className="stand-build-sha">{component.sha}</code>
      </li>)}</ul>}
    </section>
    {build.images.length > 0 && <details className="stand-build-images"><summary>Образы ({build.images.length}): идентификаторы и контрольные суммы</summary>
      <ul className="stand-detail-plain-list">{build.images.map((image, index) => <li key={`${image.id}-${index}`}>
        <code>{image.id}</code>
        {image.repo_tags.map((tag) => <p key={tag}><span className="sub">Тег: </span><code>{tag}</code></p>)}
        {image.repo_digests.map((digest) => <p key={digest}><span className="sub">Контрольная сумма: </span><code>{digest}</code></p>)}
      </li>)}</ul>
    </details>}
    {Object.keys(build.output_bindings).length > 0 && <section className="stand-build-sources" aria-label="Адреса сервисов">
      <h4>Адреса сервисов</h4>
      <ul className="stand-detail-plain-list">{Object.entries(build.output_bindings).map(([name, value]) => <li key={name}>
        <strong>{bindingTitle(name, build.output_binding_titles)}</strong>: {!removed && /^https?:\/\//i.test(value)
          ? <a href={value} target="_blank" rel="noopener noreferrer">{value} ↗</a> : <code>{value}</code>}
      </li>)}</ul>
    </section>}
    <footer className="stand-detail-row stand-build-actions">
      {build.job_id !== null && <Link to={`/jobs/${build.job_id}`}>Журнал задачи №{build.job_id}</Link>}
      {build.artifacts.map((artifact) => <a key={artifact.name} href={artifact.url} target="_blank" rel="noopener noreferrer">{
        artifact.name === "allure-report.html" ? "Отчёт Allure" : artifact.name === "results.json" ? "Результаты (JSON)" : artifact.name === "junit.xml" ? "Результаты (JUnit)" : "Результаты"
      } ↗</a>)}
      {canManage && ended && <button className="secondary" disabled={disabled} onClick={onRepeat} title="Повторно собрать и развернуть те же версии исходников">
        {pending ? "Отправка запроса…" : "Повторить по этим SHA"}
      </button>}
    </footer>
  </article>;
}
