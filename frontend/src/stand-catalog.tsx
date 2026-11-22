import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ApiError, getJson, sendJson, sendRawJson, type Project } from "./api";
import { projectApi, type StandRepository, type StandRepositoryPreview, type StandTemplate, type StandDeploymentConfiguration, type GitServer, type GitRepositoryPage, type GitRemoteRepository } from "./stand-api";
import "./stand-catalog.css";
import { GitServers } from "./git-servers";
import { CatalogDialog } from "./catalog-dialog";
import { TemplateHelp } from "./template-help";
import { initialDataBody, InitialProjectData, ProjectDataBundle, type InitialDatasetDraft } from "./stand-data";

const identifiers = /^[A-Za-z][A-Za-z0-9_.-]{0,127}$/;
const list = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
const errorText = (error: unknown) => error instanceof Error ? error.message : "Не удалось выполнить запрос";

function useCatalogLoad<T>(url: string, revision: number) {
  const [state, setState] = useState<{ data: T | null; error: string; loading: boolean }>({ data: null, error: "", loading: true });
  useEffect(() => {
    let active = true;
    setState((previous) => ({ ...previous, error: "", loading: true }));
    getJson<T>(url).then((data) => { if (active) setState({ data, error: "", loading: false }); })
      .catch((error: unknown) => { if (active) setState((previous) => ({ ...previous, error: errorText(error), loading: false })); });
    return () => { active = false; };
  }, [url, revision]);
  return state;
}

export function StandProjectCreatePage() {
  const navigate = useNavigate();
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [data, setData] = useState<InitialDatasetDraft[]>([]);
  const [settings, setSettings] = useState({ results: "", tests: "", tests_url: "", results_url: "", environments: "", runner: "newman", capability: "" });
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setError("");
    if (!code.trim() || [...code].length > 32 || /[/\\\p{C}]/u.test(code)) {
      setError("Код проекта: от 1 до 32 печатных символов, без / и \\.");
      return;
    }
    if (!title.trim()) { setError("Введите название проекта."); return; }
    const configuration: Record<string, unknown> = {};
    if (advanced) {
      for (const key of ["results", "tests"] as const) {
        const path = settings[key];
        if (path && !/^(?:\/|[A-Za-z]:[\\/]|\\\\)/.test(path)) {
          setError("Пути тестов и результатов должны быть абсолютными на сервере портала.");
          return;
        }
        if (path) configuration[key] = path;
      }
      for (const key of ["tests_url", "results_url", "capability"] as const) if (settings[key]) configuration[key] = settings[key];
      configuration.environments = list(settings.environments);
      configuration.runner = settings.runner;
    }
    setSaving(true);
    try {
      const metadata = JSON.stringify({ code, title, configuration });
      await sendRawJson<Project>("/api/projects", "POST", `${metadata.slice(0, -1)},"initial_test_data":${initialDataBody(data)}}`);
      navigate(`/stands/catalog/${encodeURIComponent(code)}`);
    } catch (error: unknown) {
      setError(error instanceof ApiError && error.status === 409 ? "Проект с таким кодом уже существует. Выберите другой код или откройте каталог существующего проекта." : errorText(error));
    } finally { setSaving(false); }
  }
  return <div className="stand-catalog">
    <header className="page-head"><h1>Новый проект</h1><Link to="/stands">К стендам</Link></header>
    <p className="sub">Создайте проект, затем добавьте репозитории и версии шаблонов для его стендов.</p>
    <form className="form stand-catalog-panel" onSubmit={submit}>
      <fieldset disabled={saving} className="stand-catalog-fields">
        <label>Код проекта<input required value={code} onChange={(event) => setCode(event.target.value)} aria-describedby="project-code-help" /></label>
        <span id="project-code-help" className="sub">До 32 символов. Можно использовать кириллицу, пробелы и двоеточие; / и \\ запрещены.</span>
        <label>Название проекта<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <InitialProjectData rows={data} setRows={setData} />
        <label className="check"><input type="checkbox" checked={advanced} onChange={(event) => setAdvanced(event.target.checked)} />Настроить тесты и результаты</label>
        {advanced && <div className="stand-catalog-fields">
          <p className="sub">Необязательно. Пути должны быть доступны процессу портала; для Docker укажите пути внутри контейнера.</p>
          <label>Путь к результатам на сервере портала<input value={settings.results} onChange={(event) => setSettings({ ...settings, results: event.target.value })} /></label>
          <label>Путь к тестам на сервере портала<input value={settings.tests} onChange={(event) => setSettings({ ...settings, tests: event.target.value })} /></label>
          <label>URL репозитория тестов<input value={settings.tests_url} onChange={(event) => setSettings({ ...settings, tests_url: event.target.value })} /></label>
          <label>URL репозитория результатов<input value={settings.results_url} onChange={(event) => setSettings({ ...settings, results_url: event.target.value })} /></label>
          <label>Окружения через запятую<input value={settings.environments} onChange={(event) => setSettings({ ...settings, environments: event.target.value })} /></label>
          <label>Исполнитель тестов<select aria-label="Исполнитель тестов" value={settings.runner} onChange={(event) => setSettings({ ...settings, runner: event.target.value })}><option value="newman">Newman</option><option value="pytest">pytest</option></select></label>
          <label>Возможность агента для прогонов<input aria-label="Возможность агента для прогонов" value={settings.capability} onChange={(event) => setSettings({ ...settings, capability: event.target.value })} /><span className="sub">Если пусто, используется имя исполнителя тестов.</span></label>
        </div>}
      </fieldset>
      {error && <p className="message error" role="alert">{error}</p>}
      <div><button className="primary" disabled={saving}>{saving ? "Создание…" : "Создать проект"}</button></div>
    </form>
  </div>;
}

export function StandCatalogPage() {
  const { project } = useParams<{ project: string }>();
  if (!project) return <p className="message error" role="alert">Не указан проект.</p>;
  return <ProjectCatalog key={project} project={project} />;
}

function ProjectCatalog({ project }: { project: string }) {
  const [revision, setRevision] = useState(0);
  const [templateRegistrationOpen, setTemplateRegistrationOpen] = useState(false);
  const [settingsTemplate, setSettingsTemplate] = useState<StandTemplate | null>(null);
  const projects = useCatalogLoad<Project[]>("/api/projects", revision);
  const repositories = useCatalogLoad<StandRepository[]>(`${projectApi(project)}/stand-repositories`, revision);
  const servers = useCatalogLoad<GitServer[]>("/api/git-servers", revision);
  const templates = useCatalogLoad<StandTemplate[]>(`${projectApi(project)}/stand-templates`, revision);
  const deployments = useCatalogLoad<StandDeploymentConfiguration[]>(`${projectApi(project)}/stand-deployment-configurations`, revision);
  const title = projects.data?.find((item) => item.code === project)?.title ?? project;
  return <div className="stand-catalog">
    <header className="page-head"><h1>Каталог стендов · {title}</h1><button className="link" onClick={() => setRevision((value) => value + 1)}>Обновить каталог</button></header>
    <div className="links"><Link to={`/stands/new?project=${encodeURIComponent(project)}`}>Создать стенд</Link><Link to="/stands">К стендам</Link></div>
    <ProjectDataBundle project={project} modules={Object.assign({}, ...(templates.data ?? []).map((template) => template.data_modules ?? {}))} />
    {projects.error && <p className="message error" role="alert">Не удалось загрузить название проекта: {projects.error}</p>}
    <GitServers servers={servers.data ?? []} loading={servers.loading} loadError={servers.error} onSaved={() => setRevision((value) => value + 1)} />
    <RepositoryEditor project={project} servers={servers.data ?? []} repositories={repositories.data ?? []} loading={repositories.loading} loadError={repositories.error} onSaved={() => setRevision((value) => value + 1)} />
    <section aria-labelledby="catalog-templates">
      <div className="stand-catalog-section-head">
        <h2 id="catalog-templates">Шаблоны стендов</h2>
        <button type="button" className="primary" onClick={() => setTemplateRegistrationOpen(true)}>Добавить шаблон</button>
      </div>
      <p className="sub">Загрузите файл шаблона portable v1. Чтобы изменить состав или сценарии сборки, зарегистрируйте новую версию.</p>
      <TemplateHelp repositories={repositories.data ?? []} deployments={deployments.data ?? []} />
      {templateRegistrationOpen && <TemplateRegistration project={project} onClose={() => setTemplateRegistrationOpen(false)} onSaved={() => setRevision((value) => value + 1)} />}
      {settingsTemplate && <TemplateWizardSettings project={project} template={settingsTemplate} onClose={() => setSettingsTemplate(null)} onSaved={() => {
        setSettingsTemplate(null);
        setRevision((value) => value + 1);
      }} />}
      {templates.loading && <p role="status">Загрузка шаблонов…</p>}
      {templates.error && <p className="message error" role="alert">Не удалось загрузить шаблоны: {templates.error}</p>}
      {!templates.loading && !templates.error && templates.data?.length === 0 && <p className="sub">Шаблоны для создания новых стендов пока не зарегистрированы.</p>}
      <div className="stand-catalog-template-list">{templates.data?.map((template) => <article className="stand-catalog-panel" key={`${template.id}:${template.version}`}>
        <h3>{template.title}</h3><p><code>{template.id}</code> · версия {template.version} · компонентов: {template.components.length}</p>
        <ProfileSummary profiles={template.profiles} />
        <button type="button" className="secondary" onClick={() => setSettingsTemplate(template)}>Компоненты мастера</button>
      </article>)}</div>
      {deployments.error && <p className="message error" role="alert">Не удалось загрузить конфигурации пересборки: {deployments.error}</p>}
      {!!deployments.data?.length && <div className="stand-catalog-panel">
        <h3>Конфигурации пересборки действующих стендов</h3>
        <p className="sub">Установка использует их для пересборки указанных стендов. Для создания нового стенда нужен отдельный шаблон portable v1.</p>
        <dl className="stand-deployment-list">{deployments.data.map((deployment) => <div key={deployment.stand}>
          <dt>{deployment.title}</dt>
          <dd>{deployment.refs.length ? deployment.refs.map((ref) => <div key={ref.name}>{ref.title} · <code>{ref.default || "указывается при пересборке"}</code></div>) : "Ревизии задаёт установка"}</dd>
        </div>)}</dl>
        <Link to="/stands">Открыть карточки стендов</Link>
      </div>}
    </section>
  </div>;
}

function validRepositoryUrl(value: string): boolean {
  try {
    const url = new URL(value.trim());
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return false;
    const last = url.pathname.split("/").filter(Boolean).at(-1);
    return Boolean(last && decodeURIComponent(last).replace(/\.git$/i, ""));
  } catch { return false; }
}

function repositoryInputUrl(value: string, server?: GitServer): string {
  const entered = value.trim();
  if (!server || !/^[A-Za-z0-9_.~-]+(?:\/[A-Za-z0-9_.~-]+)+$/.test(entered)) return entered;
  return `${server.base_url}/${entered}${/\.git$/i.test(entered) ? "" : ".git"}`;
}

function RepositoryEditor({ project, servers, repositories, loading, loadError, onSaved }: { project: string; servers: GitServer[]; repositories: StandRepository[]; loading: boolean; loadError: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [components, setComponents] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [gitUrl, setGitUrl] = useState("");
  const [serverId, setServerId] = useState("");
  const [remote, setRemote] = useState<{ items: GitRemoteRepository[]; page: number; has_next: boolean; loading: boolean; error: string }>({ items: [], page: 0, has_next: false, loading: false, error: "" });
  const [remoteRevision, setRemoteRevision] = useState(0);
  const server = servers.find((item) => item.id === serverId);
  const canList = Boolean(server && server.provider !== "other" && server.has_token && server.access === "api");
  useEffect(() => {
    let active = true;
    const load = open && canList;
    setRemote({ items: [], page: 0, has_next: false, loading: load, error: "" });
    if (load) {
      getJson<GitRepositoryPage>(`/api/git-servers/${encodeURIComponent(serverId)}/repositories?page=1`).then((page) => {
        if (active) setRemote({ ...page, loading: false, error: "" });
      }).catch(() => { if (active) setRemote({ items: [], page: 0, has_next: false, loading: false, error: "Не удалось загрузить список. Проверьте токен и его права в настройках Git-сервера или укажите URL вручную." }); });
    }
    return () => { active = false; };
  }, [open, serverId, canList, remoteRevision]);
  // Ответ старого подключения не должен дополнять список нового.
  const remoteRequest = useRef(0);
  useEffect(() => { remoteRequest.current += 1; }, [open, serverId, canList, remoteRevision]);
  async function loadMore() {
    if (!remote.has_next || remote.loading) return;
    const request = remoteRequest.current;
    setRemote((value) => ({ ...value, loading: true, error: "" }));
    try {
      const page = await getJson<GitRepositoryPage>(`/api/git-servers/${encodeURIComponent(serverId)}/repositories?page=${remote.page + 1}`);
      if (request === remoteRequest.current) setRemote((value) => ({ ...page, items: [...value.items, ...page.items.filter((item) => !value.items.some((existing) => existing.git_url === item.git_url))], loading: false, error: "" }));
    } catch { if (request === remoteRequest.current) setRemote((value) => ({ ...value, loading: false, error: "Не удалось загрузить следующую страницу. Попробуйте ещё раз." })); }
  }
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [previewRevision, setPreviewRevision] = useState(0);
  const [preview, setPreview] = useState<{ source: string; data: StandRepositoryPreview | null; error: string; loading: boolean } | null>(null);
  const [draft, setDraft] = useState<{ source: string; title: string; components: string; componentsEdited: boolean } | null>(null);
  const groups = [...servers.map((item) => ({ id: item.id, title: item.title })),
    ...[...new Set(repositories.map((item) => item.git_server_id).filter((id): id is string => Boolean(id) && !servers.some((server) => server.id === id)))].map((id) => ({ id, title: "Недоступное подключение" })),
    { id: "", title: "Без подключения (прежние записи)" }];
  const url = repositoryInputUrl(gitUrl, server);
  const source = JSON.stringify([serverId, url]);
  const currentPreview = preview?.source === source ? preview : null;
  const currentDraft = draft?.source === source ? draft : null;
  const details = editing ? { key, title, components, componentsEdited: true, suggested: false }
    : currentPreview?.data && currentDraft ? { ...currentDraft, key: currentPreview.data.key, suggested: currentPreview.data.components.length > 0 }
      : null;
  useEffect(() => {
    if (!open || editing || !serverId || !url) { setPreview(null); return; }
    let active = true;
    setPreview({ source, data: null, error: "", loading: true });
    const timer = window.setTimeout(() => {
      sendJson<StandRepositoryPreview>(`${projectApi(project)}/stand-repositories/preview`, "POST", { git_url: url, git_server_id: serverId })
        .then((data) => {
          if (!active) return;
          setPreview({ source, data, error: "", loading: false });
          const components = data.components.join(", ");
          setDraft((previous) => previous?.source === source
            ? { ...previous, components: previous.componentsEdited ? previous.components : components }
            : { source, title: data.title, components, componentsEdited: false });
        })
        .catch((error: unknown) => { if (active) setPreview({ source, data: null, error: errorText(error), loading: false }); });
    }, 350);
    return () => { active = false; window.clearTimeout(timer); };
  }, [open, editing, serverId, url, source, project, previewRevision]);
  function openEditor(repository?: StandRepository, groupId = "") {
    setKey(repository?.key ?? ""); setTitle(repository?.title ?? ""); setComponents(repository?.components.join(", ") ?? "");
    setEnabled(repository?.enabled ?? true); setEditing(Boolean(repository)); setError(""); setNotice("");
    setServerId(repository?.git_server_id ?? groupId);
    setGitUrl(repository?.git_url ?? "");
    setPreview(null); setDraft(null);
    setOpen(true);
  }
  function closeEditor() {
    setOpen(false);
  }
  function changeUrl(value: string) {
    if (repositoryInputUrl(value, server) !== url) { setPreview(null); setDraft(null); }
    setGitUrl(value); setNotice(""); setError("");
  }
  function chooseServer(id: string) {
    const next = servers.find((item) => item.id === id);
    setServerId(id); setError(""); setNotice("");
    setPreview(null); setDraft(null);
    if (next && !url.startsWith(`${next.base_url}/`)) setGitUrl("");
    else setGitUrl(url);
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving || !details) return;
    const parsed = list(details.components);
    setError(""); setNotice("");
    if ((editing && !identifiers.test(key)) || parsed.some((item) => !identifiers.test(item))) { setError("Ключ и компоненты: латинская буква в начале, затем буквы, цифры, точка, дефис или подчёркивание; до 128 символов."); return; }
    if (new Set(parsed).size !== parsed.length) { setError("Каждый компонент нужно указать только один раз."); return; }
    if (!details.title.trim()) { setError("Введите отображаемое название репозитория."); return; }
    if (!editing && !serverId) { setError("Выберите существующий Git-сервер."); return; }
    if (serverId && !url) { setError("Выберите репозиторий или укажите его путь."); return; }
    if (url && !validRepositoryUrl(url)) { setError("Укажите путь или HTTPS URL Git-репозитория без учётных данных."); return; }
    setSaving(true);
    try {
      const body = { title: details.title, ...(details.componentsEdited ? { components: parsed } : {}), enabled, git_url: url || null, git_server_id: serverId || null };
      const repository = editing
        ? await sendJson<StandRepository>(`${projectApi(project)}/stand-repositories/${encodeURIComponent(key)}`, "PUT", body)
        : await sendJson<StandRepository>(`${projectApi(project)}/stand-repositories`, "POST", body);
      setServerId(repository.git_server_id ?? ""); setKey(repository.key); setTitle(repository.title); setGitUrl(repository.git_url ?? "");
      setComponents(repository.components.join(", ")); setEnabled(repository.enabled);
      setEditing(true); setNotice("Репозиторий сохранён."); onSaved();
    } catch (error: unknown) { setError(errorText(error)); } finally { setSaving(false); }
  }
  return <section aria-labelledby="catalog-repositories">
    <div className="stand-catalog-section-head">
      <h2 id="catalog-repositories">Репозитории</h2>
      <button type="button" className="primary" onClick={() => openEditor(undefined, servers.length === 1 ? servers[0].id : "")}>Добавить репозиторий</button>
    </div>
    {loading && !repositories.length && <p role="status">Загрузка репозиториев…</p>}
    {loadError && <p className="message error" role="alert">Не удалось загрузить репозитории. {loadError}</p>}
    {!loading && !loadError && !repositories.length && <p className="sub">В проекте пока нет репозиториев.</p>}
      {groups.map((group) => {
        const entries = repositories.filter((item) => (item.git_server_id ?? "") === group.id);
        return entries.length > 0 && <div className="stand-catalog-repository-group" key={group.id}>
          <div className="stand-catalog-section-head">
            <h3>{group.title}</h3>
            {servers.some((item) => item.id === group.id) && <button type="button" className="link" onClick={() => openEditor(undefined, group.id)} aria-label={`Добавить репозиторий в ${group.title}`}>Добавить сюда</button>}
          </div>
          <ul className="stand-catalog-repositories">{entries.map((repository) => <li key={repository.key}>
          <div><strong>{repository.title}</strong> <span className={`tag ${repository.enabled ? "good" : "muted"}`}>{repository.enabled ? "Включён" : "Отключён"}</span><div className="sub">{repository.key} · компоненты: {repository.components.join(", ") || "не заданы"}</div>{repository.git_url && <div className="sub">Исходный URL: {repository.git_url}</div>}</div>
          <button type="button" className="secondary" onClick={() => openEditor(repository)} aria-label={`Изменить репозиторий ${repository.title}`}>Изменить</button>
        </li>)}</ul></div>;
      })}
    {open && <CatalogDialog title={editing ? "Изменение репозитория" : "Добавить репозиторий"} busy={saving} onClose={closeEditor}>
    <form className="form" onSubmit={submit}>
      {!editing && !servers.length && <p className="sub">Сначала добавьте подключение в разделе «Git-серверы».</p>}
      <fieldset className="stand-catalog-fields" disabled={saving}>
        <label>Git-сервер<select aria-label="Git-сервер" required={!editing} value={serverId} onChange={(event) => chooseServer(event.target.value)}><option value="">{editing ? "Без подключения (прежняя запись)" : "Выберите Git-сервер"}</option>{servers.map((item) => <option key={item.id} value={item.id}>{item.title} · {item.base_url}</option>)}</select></label>
        {canList && <>
          <label>Репозиторий из списка<select aria-label="Репозиторий из списка" value={remote.items.some((item) => item.git_url === url) ? url : ""} disabled={remote.loading && !remote.items.length} onChange={(event) => changeUrl(event.target.value)}><option value="">Выберите репозиторий</option>{remote.items.map((item) => <option key={item.git_url} value={item.git_url}>{item.path} · {item.name}</option>)}</select></label>
          {remote.loading && <p role="status">Загрузка репозиториев…</p>}
          {remote.error && <p className="message error" role="alert">{remote.error}</p>}
          {!remote.loading && !remote.error && !remote.items.length && <p className="sub">Доступных репозиториев нет. Проверьте область действия токена или укажите URL вручную.</p>}
          <div className="links">{remote.has_next && <button type="button" className="secondary" disabled={remote.loading} onClick={() => void loadMore()}>Загрузить ещё</button>}<button type="button" className="link" disabled={remote.loading} onClick={() => setRemoteRevision((value) => value + 1)}>Обновить список</button></div>
        </>}
        {(server || editing) && <label>{server ? "Путь или HTTPS URL репозитория" : "Исходный URL Git-репозитория"}<input type={server ? "text" : "url"} required={Boolean(serverId)} placeholder={server ? "group/repository" : "https://git.example.org/group/repository.git"} value={gitUrl} onChange={(event) => changeUrl(event.target.value)} /><span className="sub">{server ? "Введите путь на выбранном сервере или полный HTTPS URL клонирования." : "Введите HTTPS URL без учётных данных. Для подготовленной локальной копии поле можно оставить пустым."}{!editing && " Название и ключ определит сервер после проверки адреса."}</span></label>}
        {!editing && currentPreview?.loading && <p role="status">Проверка репозитория…</p>}
        {!editing && currentPreview?.error && <div><p className="message error" role="alert">{currentPreview.error}</p><button type="button" className="secondary" onClick={() => setPreviewRevision((value) => value + 1)}>Повторить проверку</button></div>}
        {details && <>
          <label>Ключ репозитория<input aria-label="Ключ репозитория" readOnly value={details.key} /><span className="sub">Постоянный ключ для шаблонов и каталога хостов. При совпадении имён портал учитывает группу.</span></label>
          <label>Отображаемое название<input required maxLength={200} value={details.title} onChange={(event) => editing ? setTitle(event.target.value) : setDraft((previous) => previous?.source === source ? { ...previous, title: event.target.value } : previous)} /></label>
          <label>Компоненты стенда<input aria-label="Компоненты стенда" value={details.components} onChange={(event) => editing ? setComponents(event.target.value) : setDraft((previous) => previous?.source === source ? { ...previous, components: event.target.value, componentsEdited: true } : previous)} /><span className="sub">{editing || details.suggested ? "Идентификаторы компонентов из шаблонов проекта. Список можно изменить, указав значения через запятую." : "В шаблонах проекта пока нет компонентов для этого ключа. Укажите свои через запятую или оставьте поле пустым."}</span></label>
          <label className="check"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />Репозиторий включён</label>
        </>}
      </fieldset>
      {error && <p className="message error" role="alert">{error}</p>}{notice && <p className="stand-catalog-success" role="status">{notice}</p>}
      {details && <div><button className="primary" disabled={saving}>{saving ? "Сохранение…" : "Сохранить репозиторий"}</button></div>}
    </form>
    </CatalogDialog>}
  </section>;
}

type Preview = { id: string; version: string; components: string[]; profiles: Record<string, { services: string[]; resources: { cpus: number; memory_mb: number } }> };
function record(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === "object" && !Array.isArray(value); }
function previewSpec(spec: unknown): Preview {
  if (!record(spec) || typeof spec.id !== "string" || typeof spec.version !== "string" || !Array.isArray(spec.components) || !record(spec.profiles)) throw new Error("В файле нужны id, version, список components и объект profiles шаблона portable v1.");
  const components = spec.components.map((component: unknown) => {
    if (!record(component) || typeof component.id !== "string") throw new Error("У каждого компонента шаблона должен быть строковый id.");
    return component.id;
  });
  const profiles: Preview["profiles"] = {};
  for (const [name, profile] of Object.entries(spec.profiles)) {
    if (!record(profile) || !Array.isArray(profile.services) || profile.services.some((service) => typeof service !== "string") || !record(profile.resources) || typeof profile.resources.cpus !== "number" || typeof profile.resources.memory_mb !== "number") throw new Error(`Профиль «${name}»: нужны services и ресурсы cpus, memory_mb.`);
    Object.defineProperty(profiles, name, { enumerable: true, value: { services: profile.services, resources: { cpus: profile.resources.cpus, memory_mb: profile.resources.memory_mb } } });
  }
  return { id: spec.id, version: spec.version, components, profiles };
}

function ProfileSummary({ profiles }: { profiles: Preview["profiles"] }) {
  return <ul className="stand-catalog-profiles">{Object.entries(profiles).map(([name, profile]) => <li key={name}><strong>{name}</strong>: CPU {profile.resources.cpus} · память {profile.resources.memory_mb} МБ<span className="sub"> · сервисы: {profile.services.join(", ")}</span></li>)}</ul>;
}

function TemplateWizardSettings({ project, template, onSaved, onClose }: { project: string; template: StandTemplate; onSaved: () => void; onClose: () => void }) {
  const [excluded, setExcluded] = useState(template.name_ref_exclusions ?? []);
  const [technical, setTechnical] = useState(template.technical_components ?? []);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving) return;
    setError("");
    setSaving(true);
    try {
      const path = `${projectApi(project)}/stand-templates/${encodeURIComponent(template.id)}/${encodeURIComponent(template.version)}/wizard-settings`;
      await sendJson(path, "PATCH", { name_ref_exclusions: excluded, technical_components: technical });
      onSaved();
    } catch (failure: unknown) {
      setError(errorText(failure));
    } finally {
      setSaving(false);
    }
  }
  return <CatalogDialog title="Компоненты мастера" busy={saving} onClose={onClose}>
    <form className="form" onSubmit={submit}>
      <p>{template.title} · версия {template.version}</p>
      <p className="sub">Репозиторий и ревизия технического компонента берутся из шаблона. Ветки для предложенного имени выбираются среди остальных компонентов.</p>
      <fieldset className="stand-catalog-fields" disabled={saving} aria-label="Настройки компонентов">
        <table className="stand-catalog-component-settings">
          <thead><tr><th scope="col">Компонент</th><th scope="col">Технический</th><th scope="col">Ветка в имени</th></tr></thead>
          <tbody>{template.components.map(({ id }) => {
            const fixed = technical.includes(id);
            return <tr key={id}>
              <th scope="row">{id}</th>
              <td><input type="checkbox" aria-label={`Технический компонент ${id}`} checked={fixed} onChange={(event) => {
                const checked = event.target.checked;
                setTechnical((values) => checked ? [...values, id] : values.filter((value) => value !== id));
              }} /></td>
              <td><input type="checkbox" aria-label={`Ветка ${id} в имени стенда`} checked={!fixed && !excluded.includes(id)} disabled={fixed} onChange={(event) => {
                const checked = event.target.checked;
                setExcluded((values) => checked ? values.filter((value) => value !== id) : [...values, id]);
              }} /></td>
            </tr>;
          })}</tbody>
        </table>
      </fieldset>
      {error && <p className="message error" role="alert">{error}</p>}
      <div><button className="primary" disabled={saving}>{saving ? "Сохранение…" : "Сохранить"}</button></div>
    </form>
  </CatalogDialog>;
}

function TemplateRegistration({ project, onSaved, onClose }: { project: string; onSaved: () => void; onClose: () => void }) {
  const [title, setTitle] = useState("");
  const [selected, setSelected] = useState<{ spec: Record<string, unknown>; preview: Preview } | null>(null);
  const [reading, setReading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fileVersion = useRef(0);
  useEffect(() => () => { fileVersion.current += 1; }, []);
  async function readFile(file?: File) {
    const version = ++fileVersion.current;
    setSelected(null); setError(""); setNotice(""); setReading(false);
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) { setError("Файл слишком большой. Максимальный размер — 2 МБ."); return; }
    setReading(true);
    try {
      const content = await file.text();
      if (version !== fileVersion.current) return;
      let spec: unknown;
      try { spec = JSON.parse(content); } catch { throw new Error("Не удалось прочитать JSON. Проверьте синтаксис файла и выберите его снова."); }
      const preview = previewSpec(spec);
      if (!record(spec)) return;
      setSelected({ spec, preview });
    } catch (error: unknown) { if (version === fileVersion.current) setError(errorText(error)); }
    finally { if (version === fileVersion.current) setReading(false); }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (saving || reading || !selected) return;
    setError(""); setNotice("");
    if (!title.trim()) { setError("Введите название шаблона."); return; }
    setSaving(true);
    try {
      await sendJson(`${projectApi(project)}/stand-templates`, "POST", { title, spec: selected.spec });
      setNotice("Версия шаблона зарегистрирована."); onSaved();
    } catch (error: unknown) {
      setError(error instanceof ApiError && error.status === 409 ? "Эта версия уже зарегистрирована с другим содержимым или названием. Измените версию в файле и загрузите его снова. Выбранный файл и название сохранены." : errorText(error));
    } finally { setSaving(false); }
  }
  return <CatalogDialog title="Регистрация версии шаблона" busy={saving || reading} onClose={onClose}>
    <form className="form" onSubmit={submit}>
      <fieldset className="stand-catalog-fields" disabled={saving}>
        <label>Название шаблона<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
        <label>JSON-файл шаблона<input type="file" accept=".json,application/json" onChange={(event) => { void readFile(event.target.files?.[0]); }} aria-describedby="catalog-template-file-help" /></label>
        <span id="catalog-template-file-help" className="sub">До 2 МБ. Перед сохранением проверьте версию, компоненты и лимиты ресурсов ниже.</span>
      </fieldset>
      {reading && <p role="status">Чтение файла…</p>}
      {selected && <div className="stand-catalog-review" aria-label="Проверка шаблона перед регистрацией"><h4>Проверьте выбранную версию</h4><p><code>{selected.preview.id}</code> · версия {selected.preview.version}</p><p>Компоненты ({selected.preview.components.length}): {selected.preview.components.join(", ") || "не заданы"}</p><ProfileSummary profiles={selected.preview.profiles} /><p className="sub">Полный контракт шаблона проверит сервер при регистрации.</p></div>}
      {error && <p className="message error" role="alert">{error}</p>}{notice && <p className="stand-catalog-success" role="status">{notice}</p>}
      <div><button className="primary" disabled={!selected || reading || saving}>{saving ? "Регистрация…" : "Зарегистрировать шаблон"}</button></div>
    </form>
  </CatalogDialog>;
}
