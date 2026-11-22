import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, getJson, sendJson, type Me, type Project } from "./api";
import { bindingTitle, projectApi, newIdempotencyKey, type StandDataBundle, type StandTemplate, type StandRepository, type StandHost, type StandRef, type StandNameCheck, type StandCreateRequest, type StandPreflight, type StandInstance } from "./stand-api";
import { groupTemplates, suggestStandName, type ComponentSelection } from "./stand-create-options";
import "./stand-create.css";

type Draft = { project: string; template: string; name: string | null; host: string; profile: string; components: Record<string, ComponentSelection>; parameters: Record<string, string>; environments: string; mode: "permanent" | "ephemeral"; expires: string; installTestData: boolean };
type Attempt = { project: string; body: StandCreateRequest; signature: string; key: string };
type Operation = "preview" | "checking" | "creating";
type RepositoryRefs = { refs: StandRef[]; loading: boolean; error: string };
type NameCheck = { key: string; conflict: StandNameCheck["conflict"]; error: string };
const STEPS = ["Проект и шаблон", "Компоненты", "Настройки", "Проверка"];
const templateId = (template: StandTemplate) => JSON.stringify([template.id, template.version]);
const apiPath = (project: string, path: string) => `${projectApi(project)}/${path}`;
const owns = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key);
const errorText = (error: unknown) => error instanceof Error ? error.message : "Не удалось выполнить запрос";
const humanMessage = (message: string) => /stand-template-v1|подключения исполнителя|свободного исполнителя/.test(message)
  ? "Исполнитель на выбранном хосте пока недоступен. Стенд будет ожидать подключения и готовности исполнителя." : message;
function tomorrowLocal() {
  const date = new Date(Date.now() + 24 * 60 * 60 * 1000);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function StandCreatePage({ me }: { me: Me }) {
  if (me.is_guest || !["admin", "tester", "developer"].includes(me.role)) return <p className="message" role="alert">Создание стендов доступно администратору, тестировщику и разработчику.</p>;
  return <StandCreateWizard me={me} />;
}

function StandCreateWizard({ me }: { me: Me }) {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const [draft, setDraft] = useState<Draft>(() => ({ project: search.get("project") ?? "", template: "", name: null, host: "", profile: "", components: {}, parameters: {}, environments: "", mode: me.role === "developer" ? "ephemeral" : "permanent", expires: tomorrowLocal(), installTestData: true }));
  const [projects, setProjects] = useState<Project[]>([]);
  const [hosts, setHosts] = useState<StandHost[]>([]);
  const [templates, setTemplates] = useState<StandTemplate[]>([]);
  const [repositories, setRepositories] = useState<StandRepository[]>([]);
  const [dataBundle, setDataBundle] = useState<StandDataBundle | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [loadError, setLoadError] = useState("");
  const [catalogError, setCatalogError] = useState("");
  const [reload, setReload] = useState(0);
  const [step, setStep] = useState(0);
  const [error, setError] = useState("");
  const [checked, setChecked] = useState<StandPreflight | null>(null);
  const [operation, setOperation] = useState<Operation | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [nameCheck, setNameCheck] = useState<NameCheck | null>(null);
  const [nameReload, setNameReload] = useState(0);
  const [confirmedName, setConfirmedName] = useState<{ scope: string; value: string } | null>(null);
  const preflightSequence = useRef(0);
  const submitting = useRef(false);
  const completed = useRef(false);
  const attempt = useRef<Attempt | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const errorBox = useRef<HTMLParagraphElement>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const frozen = operation === "checking" || operation === "creating" || uncertain;
  const templateGroups = useMemo(() => groupTemplates(templates), [templates]);
  const template = templates.find((item) => templateId(item) === draft.template);
  const services = template?.profiles[draft.profile]?.services ?? [];
  const profileData = (dataBundle?.entries ?? []).filter((entry) => {
    const module = template?.data_modules?.[entry.module];
    return module && services.includes(module.service);
  });
  const dataFormatMismatch = profileData.some((entry) => template?.data_modules?.[entry.module].format !== entry.format);
  const versions = templateGroups.find((items) => items[0].id === template?.id) ?? [];
  const technicalIds = new Set(template?.technical_components ?? []);
  const componentChoices = { ...draft.components };
  for (const component of template?.components ?? []) {
    if (technicalIds.has(component.id)) {
      componentChoices[component.id] = { repository: component.repository, ref: component.default_ref };
    }
  }
  const baseName = draft.name ?? suggestStandName(draft.project, componentChoices, repositories, [...(template?.name_ref_exclusions ?? []), ...technicalIds]);
  const nameScope = JSON.stringify([draft.project, baseName.trim()]);
  const name = confirmedName?.scope === nameScope ? confirmedName.value : baseName;
  const nameQuery = new URLSearchParams({ name: name.trim() });
  if (name !== baseName) nameQuery.set("base_name", baseName.trim());
  const nameCheckUrl = template && name.trim() && (step === 0 || step === 3)
    ? `${apiPath(draft.project, "stand-instance-name")}?${nameQuery}` : "";
  const nameCheckKey = JSON.stringify([step, nameReload, nameCheckUrl]);
  const currentNameCheck = nameCheck?.key === nameCheckKey ? nameCheck : null;
  const nameChecking = !!nameCheckUrl && !currentNameCheck;
  const nameBlocked = nameChecking || !!currentNameCheck?.conflict || !!currentNameCheck?.error;

  function invalidate() {
    preflightSequence.current += 1;
    setChecked(null);
    setOperation((current) => current === "preview" ? null : current);
    setError("");
  }
  function update(patch: Partial<Draft>) {
    if (submitting.current || uncertain || completed.current) return;
    invalidate();
    setDraft((previous) => ({ ...previous, ...patch }));
  }
  useEffect(() => {
    let active = true;
    setInitialLoading(true);
    setLoadError("");
    Promise.all([getJson<Project[]>("/api/projects"), getJson<StandHost[]>("/api/stand-hosts")]).then(([projectItems, hostItems]) => {
      if (!active) return;
      setProjects(projectItems);
      setHosts(hostItems);
      if (hostItems.length === 1) {
        invalidate();
        setDraft((previous) => ({ ...previous, host: hostItems[0].id }));
      }
    }).catch((failure: unknown) => { if (active) setLoadError(errorText(failure)); }).finally(() => { if (active) setInitialLoading(false); });
    return () => { active = false; };
  }, [reload]);
  useEffect(() => {
    let active = true;
    setTemplates([]);
    setRepositories([]);
    setDataBundle(null);
    setCatalogError("");
    if (!draft.project) { setCatalogLoading(false); return; }
    setCatalogLoading(true);
    Promise.all([
      getJson<StandTemplate[]>(apiPath(draft.project, "stand-templates")),
      getJson<StandRepository[]>(apiPath(draft.project, "stand-repositories")),
      getJson<StandDataBundle>(apiPath(draft.project, "stand-data-bundle")),
    ]).then(([items, repos, bundle]) => {
      if (active) {
        setTemplates(items);
        setRepositories(repos);
        setDataBundle(bundle);
        const groups = groupTemplates(items);
        if (groups.length === 1) {
          invalidate();
          setDraft((previous) => previous.template ? previous : { ...previous, ...templateDefaults(groups[0][0], repos) });
        }
      }
    }).catch((failure: unknown) => { if (active) setCatalogError(errorText(failure)); }).finally(() => { if (active) setCatalogLoading(false); });
    return () => { active = false; };
  }, [draft.project, reload]);
  useEffect(() => {
    if (!nameCheckUrl) return;
    let active = true;
    const timer = window.setTimeout(() => {
      getJson<StandNameCheck>(nameCheckUrl).then(({ conflict }) => {
        if (active) setNameCheck({ key: nameCheckKey, conflict, error: "" });
      }).catch((failure: unknown) => {
        if (active) setNameCheck({ key: nameCheckKey, conflict: null, error: errorText(failure) });
      });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [nameCheckUrl, nameCheckKey]);
  useEffect(() => { heading.current?.focus(); }, [step]);
  useEffect(() => { if (error) errorBox.current?.focus(); }, [error]);
  useEffect(() => () => { preflightSequence.current += 1; }, []);
  useEffect(() => {
    if (!frozen) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [frozen]);

  function chooseProject(project: string) {
    setConfirmedName(null);
    update({ project, template: "", profile: "", components: {}, parameters: {}, environments: "", installTestData: true });
  }
  function chooseTemplate(selected?: StandTemplate) {
    update(selected ? templateDefaults(selected, repositories) : { template: "", components: {}, parameters: {}, profile: "" });
  }
  function editName(value: string | null) {
    setConfirmedName(null);
    update({ name: value });
  }
  function confirmName(value: string) {
    invalidate();
    setConfirmedName({ scope: nameScope, value });
    nameInput.current?.focus();
  }
  function refreshNameAfterConflict(failure: unknown) {
    if (failure instanceof ApiError && failure.status === 409) setNameReload((value) => value + 1);
  }
  function validate(upTo: number): string {
    if (!projects.some((project) => project.code === draft.project)) return "Выберите проект из списка.";
    if (catalogLoading || catalogError || !template) return "Выберите доступный шаблон стенда.";
    if (!name.trim()) return "Введите имя стенда.";
    if (nameBlocked) return "Завершите проверку имени стенда.";
    if (upTo < 1) return "";
    if (!hosts.some((host) => host.id === draft.host)) return "Выберите хост для стенда.";
    for (const component of template.components) {
      const choice = componentChoices[component.id];
      const fixed = technicalIds.has(component.id);
      if (!choice || !repositories.some((repo) => repo.key === choice.repository && repo.enabled && repo.components.includes(component.id))) {
        return fixed
          ? `Репозиторий «${component.repository}» для технического компонента «${component.id}» недоступен. Обратитесь к администратору.`
          : `Для компонента «${component.id}» выберите доступный репозиторий. Если список пуст, обратитесь к администратору.`;
      }
      if (!(fixed ? choice.ref : choice.ref.trim())) return `Для компонента «${component.id}» укажите ветку, тег или SHA.`;
    }
    if (upTo < 2) return "";
    if (!owns(template.profiles, draft.profile)) return "Выберите профиль ресурсов.";
    for (const [name, spec] of Object.entries(template.parameters)) {
      const raw = draft.parameters[name] ?? "";
      if (spec.enum) {
        if (raw === "" || !/^\d+$/.test(raw) || !owns(spec.enum, raw)) return `Выберите значение параметра «${name}».`;
        const selected = spec.enum[Number(raw)];
        if ((spec.type === "integer" && (typeof selected !== "number" || !Number.isSafeInteger(selected))) || (spec.type === "number" && (typeof selected !== "number" || !Number.isFinite(selected)))) return `Параметр «${name}»: выбранное значение нельзя безопасно передать. Обратитесь к администратору для исправления шаблона.`;
      } else if (spec.type === "boolean") {
        if (raw !== "true" && raw !== "false") return `Выберите значение параметра «${name}».`;
      } else if (spec.type === "integer" || spec.type === "number") {
        if (!raw.trim() || !Number.isFinite(Number(raw)) || (spec.type === "integer" && !Number.isSafeInteger(Number(raw)))) return `Параметр «${name}»: ${spec.type === "integer" ? "введите целое число в безопасном диапазоне" : "введите конечное число"}.`;
      } else if (!raw && !owns(spec, "default")) return `Заполните параметр «${name}».`;
    }
    const environments = draft.environments.split(/[,\n]/).map((item) => item.trim()).filter(Boolean);
    if (environments.some((item) => !/^[A-Za-z][A-Za-z0-9_.-]{0,127}$/.test(item))) return "Окружения: используйте идентификаторы с латинской буквы, затем латинские буквы, цифры, _, . или - (до 128 символов).";
    if (me.role === "developer" && draft.mode !== "ephemeral") return "Разработчику доступны только временные стенды.";
    if (draft.mode === "ephemeral" && (!draft.expires || !Number.isFinite(new Date(draft.expires).getTime()) || new Date(draft.expires).getTime() <= Date.now())) return "Укажите будущую дату окончания временного стенда.";
    return "";
  }
  function buildBody(): StandCreateRequest {
    const parameters: Record<string, string | number | boolean> = {};
    for (const [name, spec] of Object.entries(template!.parameters)) {
      const raw = draft.parameters[name] ?? "";
      parameters[name] = spec.enum ? spec.enum[Number(raw)] : spec.type === "boolean" ? raw === "true" : spec.type === "integer" || spec.type === "number" ? Number(raw) : raw;
    }
    const components = Object.fromEntries(template!.components.map(({ id }) => {
      const { repository, ref } = componentChoices[id];
      return [id, { repository, ref: technicalIds.has(id) ? ref : ref.trim() }];
    }));
    return { name: name.trim(), template_key: template!.id, template_version: template!.version, target_host: draft.host, profile: draft.profile,
      components, parameters,
      environments: [...new Set(draft.environments.split(/[,\n]/).map((item) => item.trim()).filter(Boolean))], mode: draft.mode,
      expires: draft.mode === "ephemeral" ? new Date(draft.expires).toISOString() : null, ports: {}, install_test_data: draft.installTestData };
  }
  function next() {
    const message = validate(step);
    if (message) { setError(message); return; }
    setError("");
    setStep((previous) => Math.min(previous + 1, 3));
  }
  async function preflight(project: string, body: StandCreateRequest, phase: "preview" | "checking") {
    const sequence = ++preflightSequence.current;
    setChecked(null);
    setError("");
    setOperation(phase);
    try {
      const result = await sendJson<StandPreflight>(apiPath(project, "stand-instances/preflight"), "POST", body);
      if (sequence !== preflightSequence.current) return false;
      if (!result.valid) {
        setError("Конфигурация не прошла проверку. Исправьте настройки и попробуйте снова.");
        return false;
      }
      setChecked(result);
      return true;
    } catch (failure) {
      if (sequence === preflightSequence.current) {
        refreshNameAfterConflict(failure);
        setError(humanMessage(errorText(failure)));
      }
      return false;
    } finally {
      if (phase === "preview" && sequence === preflightSequence.current) setOperation(null);
    }
  }
  async function preview() {
    if (operation || submitting.current || uncertain || completed.current) return;
    const message = validate(2);
    if (message) { setError(message); return; }
    await preflight(draft.project, buildBody(), "preview");
  }

  async function create() {
    if (operation || submitting.current || completed.current) return;
    let request = attempt.current;
    if (!uncertain) {
      const message = validate(2);
      if (message) { setError(message); return; }
      const body = buildBody();
      const signature = JSON.stringify(body);
      if (!request || request.project !== draft.project || request.signature !== signature) {
        request = { project: draft.project, body, signature, key: newIdempotencyKey() };
      }
    }
    if (!request) return;
    submitting.current = true;
    try {
      if (!uncertain && !(await preflight(request.project, request.body, "checking"))) return;
      attempt.current = request;
      setOperation("creating");
      setError("");
      const result = await sendJson<StandInstance>(apiPath(request.project, "stand-instances"), "POST", request.body, { "Idempotency-Key": request.key });
      completed.current = true;
      setUncertain(false);
      navigate(`/stands/${encodeURIComponent(request.project)}/${encodeURIComponent(result.id)}`, { replace: true });
    } catch (failure) {
      const ambiguous = !(failure instanceof ApiError) || failure.status >= 500 || failure.status === 408;
      if (ambiguous) {
        setUncertain(true);
        setError("Сервер мог принять запрос, но подтверждение не получено. Настройки сохранены и временно заблокированы. Повторите исходный запрос: портал использует тот же ключ и не создаст второй стенд.");
      } else {
        setUncertain(false);
        invalidate();
        refreshNameAfterConflict(failure);
        setError(`${humanMessage(errorText(failure))} Значения сохранены. Проверьте настройки и повторите создание стенда.`);
      }
    } finally {
      submitting.current = false;
      setOperation(null);
    }
  }
  const creationProgress = operation === "checking" ? "Проверяем конфигурацию…" : operation === "creating" ? "Создаём стенд…" : "";
  const adminHelp = (text: string) => <p className="sub">{text} {me.role === "admin" && draft.project ? <Link to={`/stands/catalog/${encodeURIComponent(draft.project)}`}>Настроить каталог проекта</Link> : "Обратитесь к администратору портала."}</p>;
  const conflict = currentNameCheck?.conflict;
  const nameField = <div className="stand-create-name">
    <label>Имя стенда<input ref={nameInput} value={name} maxLength={200} autoComplete="off" onChange={(event) => editName(event.target.value)} aria-describedby="stand-name-help" /></label>
    <p id="stand-name-help" className="sub">Имя подставляется по проекту, репозиториям и веткам. Его можно изменить.</p>
    {(draft.name !== null || confirmedName?.scope === nameScope) && <button type="button" onClick={() => editName(null)}>Вернуть предложенное имя</button>}
    {nameChecking && <p className="sub" role="status">Проверяем имя…</p>}
    {currentNameCheck?.error && <div className="stand-create-name-conflict">
      <p role="alert">Не удалось проверить имя. {currentNameCheck.error}</p>
      <button type="button" onClick={() => setNameReload((value) => value + 1)}>Проверить имя снова</button>
    </div>}
    {conflict && <div className="stand-create-name-conflict">
      <p role="status">В проекте уже есть стенд «{conflict.name}». Создать ещё один под именем «{conflict.suggested_name}»?</p>
      <div>
        <button type="button" onClick={() => confirmName(conflict.suggested_name)}>Подтвердить дубль</button>
        <button type="button" onClick={() => { nameInput.current?.focus(); nameInput.current?.select(); }}>Изменить имя</button>
      </div>
    </div>}
  </div>;

  return <section className="stand-create">
    <header className="page-head"><h1>Создать стенд</h1>{!frozen && <Link to="/stands">К стендам</Link>}</header>
    <p className="sub">Выберите исходный код и ресурсы. Перед созданием портал проверит конфигурацию и доступность портов.</p>
    <ol className="stand-create-steps" aria-label="Этапы создания стенда">{STEPS.map((title, index) => <li key={title} aria-current={step === index ? "step" : undefined}><span>{index + 1}</span>{title}</li>)}</ol>
    {initialLoading && <p role="status">Загрузка проектов и хостов…</p>}
    {loadError && <p className="message error" role="alert">Не удалось загрузить каталог: {loadError} <button type="button" disabled={frozen} onClick={() => { invalidate(); setReload((value) => value + 1); }}>Повторить загрузку</button></p>}
    {error && <p className="stand-create-error" ref={errorBox} tabIndex={-1} role="alert">{error}</p>}
    <form onSubmit={(event) => { event.preventDefault(); if (step < 3 && !frozen) next(); }}>
      <h2 tabIndex={-1} ref={heading}>{step + 1}. {STEPS[step]}</h2>
      <fieldset className="stand-create-fields" disabled={frozen || initialLoading}>
        <legend className="stand-create-sr">{STEPS[step]}</legend>
        {step === 0 && <>
          <label>Проект<select aria-label="Проект" value={draft.project} onChange={(event) => chooseProject(event.target.value)}><option value="">Выберите проект</option>{projects.map((project) => <option key={project.code} value={project.code}>{project.title} ({project.code})</option>)}</select></label>
          {!initialLoading && projects.length === 0 && <p>В установке пока нет проектов. {me.role === "admin" ? <Link to="/stands/projects/new">Создать проект</Link> : "Обратитесь к администратору портала."}</p>}
          {me.role === "admin" && projects.length > 0 && <Link to="/stands/projects/new">Создать другой проект</Link>}
          {catalogLoading && <p role="status">Загрузка шаблонов и репозиториев…</p>}
          {catalogError && <p className="message error" role="alert">Не удалось загрузить каталог проекта: {catalogError} <button type="button" onClick={() => { invalidate(); setReload((value) => value + 1); }}>Повторить загрузку</button></p>}
          {templateGroups.length > 1 && <label>Шаблон сборки<select value={template?.id ?? ""} onChange={(event) => chooseTemplate(templateGroups.find((items) => items[0].id === event.target.value)?.[0])}>
            <option value="">Выберите шаблон</option>{templateGroups.map(([item]) => <option key={item.id} value={item.id}>{item.title}</option>)}
          </select></label>}
          {draft.project && !catalogLoading && !catalogError && templates.length === 0 && adminHelp("В проекте пока нет шаблонов для создания нового стенда.")}
          {templateGroups.length > 0 && <p className="sub">Шаблон задаёт состав стенда, порядок запуска и проверки после развёртывания.</p>}
          {template && <>
            <TemplateOverview template={template} versions={versions} onSelect={chooseTemplate} />
            {nameField}
          </>}
        </>}
        {step === 1 && <>
          <label>Хост<select aria-label="Хост" value={draft.host} onChange={(event) => update({ host: event.target.value })}><option value="">Выберите хост</option>{hosts.map((host) => <option key={host.id} value={host.id}>{host.title} ({host.id})</option>)}</select></label>
          {hosts.length === 1 && <p className="sub">Единственный доступный хост выбран автоматически: {hosts[0].title}.</p>}
          {hosts.length === 0 && adminHelp("Нет доступных хостов для размещения стенда.")}
          {template && <ComponentSources key={JSON.stringify([draft.project, draft.host, draft.template])} project={draft.project} host={draft.host} template={template} repositories={repositories} choices={componentChoices} onChange={(components) => update({ components })} adminHelp={adminHelp} />}

        </>}
        {step === 2 && template && <>
          <label>Профиль ресурсов<select aria-label="Профиль ресурсов" value={draft.profile} onChange={(event) => update({ profile: event.target.value })}>{Object.entries(template.profiles).map(([name, profile]) => <option key={name} value={name}>{name} · {profile.resources.cpus} CPU, {profile.resources.memory_mb} МБ на контейнер</option>)}</select></label>
          <p className="sub">Лимиты применяются к каждому контейнеру, а не ко всему стенду. Сервисы: {template.profiles[draft.profile]?.services.join(", ") || "—"}.</p>
          {Object.entries(template.parameters).map(([name, spec]) => <label key={name}>{spec.title ?? name}{!owns(spec, "default") ? " (обязательный)" : ""}
            {spec.enum ? <select aria-label={`${spec.title ?? name}${!owns(spec, "default") ? " (обязательный)" : ""}`} value={draft.parameters[name] ?? ""} onChange={(event) => update({ parameters: { ...draft.parameters, [name]: event.target.value } })}><option value="">Выберите значение</option>{spec.enum.map((value, index) => <option key={index} value={String(index)}>{String(value)}</option>)}</select>
              : spec.type === "boolean" ? <select aria-label={`${spec.title ?? name}${!owns(spec, "default") ? " (обязательный)" : ""}`} value={draft.parameters[name] ?? ""} onChange={(event) => update({ parameters: { ...draft.parameters, [name]: event.target.value } })}><option value="">Выберите значение</option><option value="true">Да</option><option value="false">Нет</option></select>
              : <input aria-label={`${spec.title ?? name}${!owns(spec, "default") ? " (обязательный)" : ""}`} value={draft.parameters[name] ?? ""} inputMode={spec.type === "integer" ? "numeric" : spec.type === "number" ? "decimal" : "text"} autoComplete="off" onChange={(event) => update({ parameters: { ...draft.parameters, [name]: event.target.value } })} />}
            {spec.description && <span className="sub">{spec.description}</span>}
          </label>)}
          <label>Окружения тестов<textarea aria-label="Окружения тестов" value={draft.environments} rows={3} onChange={(event) => update({ environments: event.target.value })} aria-describedby="stand-create-environments-help" placeholder="feature-a, regression" /></label>
          <p className="sub" id="stand-create-environments-help">Необязательно. Идентификаторы через запятую или с новой строки; повторения объединяются. Имя стенда не становится окружением автоматически.</p>
          <section aria-labelledby="stand-create-data-title">
            <h3 id="stand-create-data-title">Тестовые данные</h3>
            {profileData.length > 0 ? <>
              <label className="check"><input type="checkbox" checked={draft.installTestData} onChange={(event) => update({ installTestData: event.target.checked })} />Установить комплект «{dataBundle?.title}»</label>
              {draft.installTestData && <ul>{profileData.map((entry) => <li key={entry.module}>
                <strong>{template.data_modules?.[entry.module].title}</strong> · {entry.title} · версия {entry.version}
              </li>)}</ul>}
              {draft.installTestData && dataFormatMismatch && <p className="stand-create-warning">Формат данных одного из модулей отличается от формата шаблона. Выберите совместимую версию шаблона или снимите флажок установки.</p>}
            </> : <p className="sub">Для этого шаблона и профиля комплект данных пока пуст.</p>}
          </section>
          <label>Срок использования<select aria-label="Срок использования" value={draft.mode} onChange={(event) => update({ mode: event.target.value as Draft["mode"] })}>{me.role !== "developer" && <option value="permanent">Постоянный стенд</option>}<option value="ephemeral">Временный стенд</option></select></label>
          {me.role === "developer" && <p className="sub">Разработчику доступны только временные стенды.</p>}
          {draft.mode === "ephemeral" && <><label>Срок окончания (местное время)<input aria-label="Срок окончания (местное время)" type="datetime-local" value={draft.expires} onChange={(event) => update({ expires: event.target.value })} /></label><p className="sub">Дата сохраняется с учётом вашего часового пояса. Автоматическая остановка по сроку появится на отдельном этапе; пока завершение работы нужно контролировать вручную.</p></>}
        </>}
        {step === 3 && template && <>
          {nameField}
          <dl className="stand-create-summary">
            <dt>Проект</dt><dd>{projects.find((project) => project.code === draft.project)?.title} ({draft.project})</dd>
            <dt>Шаблон</dt><dd>{template.title} · {template.version}</dd>
            <dt>Хост</dt><dd>{hosts.find((host) => host.id === draft.host)?.title} ({draft.host})</dd>
            <dt>Профиль</dt><dd>{draft.profile}: {template.profiles[draft.profile]?.resources.cpus} CPU, {template.profiles[draft.profile]?.resources.memory_mb} МБ на каждый контейнер</dd>
            <dt>Окружения</dt><dd>{[...new Set(draft.environments.split(/[,\n]/).map((item) => item.trim()).filter(Boolean))].join(", ") || "Не заданы"}</dd>
            <dt>Тестовые данные</dt><dd>{!draft.installTestData ? "Без установки комплекта" : profileData.length > 0
              ? `${checked?.data_bundle?.title ?? dataBundle?.title} · ${(checked?.data_bundle?.entries ?? profileData).map((entry) => template.data_modules?.[entry.module]?.title ?? entry.module).join(", ")}`
              : "Комплект не настроен для профиля"}</dd>
            <dt>Срок</dt><dd>{draft.mode === "permanent" ? "Постоянный" : `До ${new Date(draft.expires).toLocaleString("ru-RU")}`}</dd>
            {template.components.map((component) => <SummaryComponent key={component.id} id={component.id} choice={componentChoices[component.id]} />)}
            {Object.entries(template.parameters).map(([name, spec]) => <SummaryParameter key={name} name={spec.title ?? name} value={spec.enum ? String(spec.enum[Number(draft.parameters[name])]) : spec.type === "boolean" ? draft.parameters[name] === "true" ? "Да" : "Нет" : draft.parameters[name]} />)}
            <dt>Порты</dt><dd>Автоматический выбор. Проверка покажет предварительные порты; окончательное резервирование — при создании.</dd>
            {Object.keys(template.output_binding_titles ?? {}).length > 0 && <><dt>Адреса сервисов</dt><dd>{Object.values(template.output_binding_titles ?? {}).join(", ")}. Адреса появятся после развёртывания.</dd></>}
          </dl>
          <button type="button" onClick={preview} disabled={!!operation || frozen || nameBlocked}>{operation === "preview" ? "Проверка…" : "Проверить конфигурацию"}</button>
          {checked && <div className="stand-create-check" role="status"><h3>Конфигурация прошла проверку</h3>
            <p>{checked.executor_available ? "Исполнитель на хосте доступен. Сборка начнётся, когда он будет свободен." : "Исполнитель на хосте пока недоступен. Созданный стенд будет ожидать его подключения."}</p>
            <p>Лимит на контейнер: {checked.resources.cpus} CPU, {checked.resources.memory_mb} МБ.</p>
            <p>Предварительные порты: {Object.entries(checked.ports).map(([name, port]) => `${bindingTitle(name, checked.output_binding_titles ?? template.output_binding_titles)}: ${port}`).join(", ") || "не требуются"}.</p>
            <p className="sub">SHA ниже — текущее наблюдение при проверке. Окончательный снимок кода будет зафиксирован исполнителем.</p>
            <ul>{checked.components.map((component) => <li key={component.id}>{component.id}: <span className="stand-create-sha">{component.observed_sha}</span></li>)}</ul>
            {checked.warnings.filter((warning) => !/stand-template-v1|подключения исполнителя|свободного исполнителя/.test(warning)).map((warning, index) => <p className="stand-create-warning" key={index}>{humanMessage(warning)}</p>)}
          </div>}
        </>}
      </fieldset>
      <div className="stand-create-actions">
        {step > 0 && <button type="button" disabled={frozen} onClick={() => { setError(""); setStep((value) => value - 1); }}>Назад</button>}
        {step < 3 ? <button type="submit" disabled={initialLoading || catalogLoading || frozen || nameBlocked}>Далее</button> : <>
          <span className="stand-create-sr" role="status">{creationProgress}</span>
          <button type="button" className="primary" disabled={!!operation || completed.current || (!uncertain && (initialLoading || catalogLoading || !template || nameBlocked))} onClick={create}>
            {creationProgress || (uncertain ? "Повторить исходный запрос" : "Создать стенд")}
          </button>
        </>}
      </div>
    </form>
  </section>;
}

function TemplateOverview({ template, versions, onSelect }: { template: StandTemplate; versions: StandTemplate[]; onSelect: (template?: StandTemplate) => void }) {
  const addresses = [...new Set(Object.values(template.output_binding_titles ?? {}))];
  const recommended = versions[0]?.version;
  return <div className="stand-create-template">
    <h3>{template.title}</h3>
    <dl className="stand-create-summary">
      <dt>Компоненты</dt><dd>{template.components.map((component) => component.id).join(", ")}</dd>
      {addresses.length > 0 && <><dt>Адреса после запуска</dt><dd>{addresses.join(", ")}</dd></>}
    </dl>
    {versions.length > 1 ? <details>
      <summary>Версия {template.version}{template.version === recommended ? " (рекомендуемая)" : ""} · выбрать другую</summary>
      <label>Версия шаблона<select value={template.version} onChange={(event) => onSelect(versions.find((item) => item.version === event.target.value))}>
        {versions.map((item) => <option key={item.version} value={item.version}>{item.version}{item.version === recommended ? " (рекомендуемая)" : ""}</option>)}
      </select></label>
      <p className="sub">Выберите предыдущую версию, если нужно повторить прежнюю конфигурацию.</p>
    </details> : <p className="sub">Версия {template.version}</p>}
  </div>;
}

function SummaryComponent({ id, choice }: { id: string; choice?: ComponentSelection }) {
  return <><dt>Компонент {id}</dt><dd>{choice?.repository} · {choice?.ref}</dd></>;
}
function SummaryParameter({ name, value }: { name: string; value: string | undefined }) {
  return <><dt>Параметр {name}</dt><dd>{value === "" ? "(пустая строка)" : value}</dd></>;
}
function templateDefaults(selected: StandTemplate, repositories: StandRepository[]): Pick<Draft, "template" | "components" | "parameters" | "profile"> {
  const components: Record<string, ComponentSelection> = {};
  const parameters: Record<string, string> = {};
  selected.components.forEach((component) => {
    const compatible = repositories.find((repository) => repository.key === component.repository && repository.enabled && repository.components.includes(component.id));
    components[component.id] = { repository: compatible?.key ?? "", ref: component.default_ref ?? "" };
  });
  Object.entries(selected.parameters).forEach(([name, spec]) => {
    parameters[name] = owns(spec, "default") ? spec.enum ? String(spec.enum.findIndex((item) => Object.is(item, spec.default))) : String(spec.default) : "";
  });
  return { template: templateId(selected), components, parameters, profile: Object.keys(selected.profiles)[0] ?? "" };
}

function ComponentSources({ project, host, template, repositories, choices, onChange, adminHelp }: {
  project: string; host: string; template: StandTemplate; repositories: StandRepository[]; choices: Record<string, ComponentSelection>;
  onChange: (choices: Record<string, ComponentSelection>) => void; adminHelp: (text: string) => React.ReactNode;
}) {
  const [branch, setBranch] = useState("");
  const [applied, setApplied] = useState("");
  const [reload, setReload] = useState(0);
  const requests = useRef(new Map<string, Promise<StandRef[]>>());
  const technicalIds = new Set(template.technical_components ?? []);
  const editableComponents = template.components.filter((component) => !technicalIds.has(component.id));
  const editableCount = editableComponents.length;
  const repositoryKeys = JSON.stringify([...new Set(editableComponents.map((component) => choices[component.id]?.repository).filter(Boolean))].sort());
  const assignments = JSON.stringify(editableComponents.map((component) => [component.id, choices[component.id]?.repository]));
  const [loaded, setLoaded] = useState<{ key: string; repositories: Record<string, RepositoryRefs> }>({ key: "", repositories: {} });
  const requestKey = JSON.stringify([project, host, repositoryKeys, reload]);
  const refsByRepository = loaded.key === requestKey ? loaded.repositories : {};
  useEffect(() => { setBranch(""); setApplied(""); }, [assignments]);
  useEffect(() => {
    let active = true;
    const keys: string[] = JSON.parse(repositoryKeys);
    setBranch("");
    setApplied("");
    if (!host) return;
    setLoaded({ key: requestKey, repositories: Object.fromEntries(keys.map((key) => [key, { refs: [], loading: true, error: "" }])) });
    keys.forEach((repository) => {
      const key = JSON.stringify([project, host, repository, reload]);
      let request = requests.current.get(key);
      if (!request) {
        request = getJson<StandRef[]>(`${apiPath(project, `stand-repositories/${encodeURIComponent(repository)}/refs`)}?host=${encodeURIComponent(host)}`);
        requests.current.set(key, request);
      }
      request
        .then((refs) => { if (active) setLoaded((previous) => ({ key: requestKey, repositories: { ...previous.repositories, [repository]: { refs, loading: false, error: "" } } })); })
        .catch((failure: unknown) => { if (active) setLoaded((previous) => ({ key: requestKey, repositories: { ...previous.repositories, [repository]: { refs: [], loading: false, error: errorText(failure) } } })); });
    });
    return () => { active = false; };
  }, [project, host, repositoryKeys, reload, requestKey]);
  const complete = !!host && editableComponents.every((component) => {
    const repository = choices[component.id]?.repository;
    return repository && refsByRepository[repository] && !refsByRepository[repository].loading && !refsByRepository[repository].error;
  });
  const branches = [...new Set(Object.values(refsByRepository).flatMap((item) => item.refs.filter((ref) => ref.kind === "branch").map((ref) => ref.name)))].sort();
  const matching = (name: string) => editableComponents.filter((component) => refsByRepository[choices[component.id]?.repository]?.refs.some((ref) => ref.kind === "branch" && ref.name === name));
  const targets = complete && branch ? matching(branch) : [];
  const remaining = editableComponents.filter((component) => !targets.includes(component));
  return <>
    {editableCount > 0 && <fieldset className="stand-create-component">
      <legend>Ветка для компонентов</legend>
      <p className="sub">Примените ветку к настраиваемым компонентам, в репозиториях которых она есть. Отдельные ветки, теги и SHA можно выбрать ниже.</p>
      <label>Ветка<select aria-label="Ветка для компонентов" value={complete ? branch : ""} disabled={!complete} onChange={(event) => { setBranch(event.target.value); setApplied(""); }}>
        <option value="">Выберите ветку</option>
        {complete && branches.map((name) => { const count = matching(name).length; return <option key={name} value={name}>{name} · {count === editableCount ? "все настраиваемые" : `${count} из ${editableCount} компонентов`}</option>; })}
      </select></label>
      {!host ? <p className="sub">Сначала выберите хост.</p> : editableComponents.some((component) => !choices[component.id]?.repository) ? <p className="sub">Выберите репозитории для настраиваемых компонентов.</p> : !complete && !Object.values(refsByRepository).some((item) => item.error) ? <p className="sub" role="status">Загрузка веток выбранных репозиториев…</p> : null}
      {Object.entries(refsByRepository).filter(([, item]) => item.error).map(([repository, item]) => <p key={repository} className="stand-create-warning" role="alert">Не удалось загрузить ветки {repository}: {item.error}. Можно указать ref вручную ниже.</p>)}
      {Object.values(refsByRepository).some((item) => item.error) && <button type="button" onClick={() => setReload((value) => value + 1)}>Повторить загрузку refs</button>}
      {complete && branches.length === 0 && <p className="sub">В выбранных репозиториях нет веток. Укажите тег или SHA для каждого компонента ниже.</p>}
      {targets.length > 0 && <>
        <p className="sub">Будут изменены: {targets.map((component) => component.id).join(", ")}.</p>
        {remaining.length > 0 && <p className="sub">Сохранят текущие refs: {remaining.map((component) => `${component.id} (${choices[component.id]?.ref || "не задан"})`).join(", ")}. Ветки «{branch}» в их репозиториях нет.</p>}
        <button type="button" onClick={() => {
          const components = { ...choices };
          targets.forEach((component) => { components[component.id] = { ...components[component.id], ref: `refs/heads/${branch}` }; });
          onChange(components);
          setApplied(`Ветка «${branch}» применена к ${targets.length} из ${editableCount} компонентов.`);
        }}>Применить к {targets.length} из {editableCount} компонентов</button>
      </>}
      {applied && <p className="sub" role="status">{applied}</p>}
    </fieldset>}
    {editableComponents.map((component) => {
      const choice = choices[component.id] ?? { repository: "", ref: "" };
      const compatible = repositories.filter((repository) => repository.enabled && repository.components.includes(component.id));
      const dependencies = component.dependencies?.filter((id) => !technicalIds.has(id));
      return <details key={component.id} className="stand-create-component stand-create-source" open>
        <summary>Компонент {component.id}</summary>
        <div className="stand-create-source-fields">
          {dependencies?.length ? <p className="sub">Зависит от {dependencies.join(", ")}</p> : null}
          <label>Репозиторий для {component.id}<select aria-label={`Репозиторий для ${component.id}`} value={choice.repository} onChange={(event) => onChange({ ...choices, [component.id]: { repository: event.target.value, ref: "" } })}><option value="">Выберите репозиторий</option>{compatible.map((repository) => <option key={repository.key} value={repository.key}>{repository.title} ({repository.key})</option>)}</select></label>
          {compatible.length === 0 && adminHelp(`Нет включённых репозиториев для компонента «${component.id}».`)}
          <RefInput host={host} repository={choice.repository} component={component.id} value={choice.ref} loaded={refsByRepository[choice.repository]} onChange={(ref) => { setApplied(""); onChange({ ...choices, [component.id]: { ...choice, ref } }); }} />
        </div>
      </details>;
    })}
    {template.components.filter((component) => technicalIds.has(component.id)).map((component) => {
      const repository = repositories.find((item) => item.key === component.repository);
      return <details key={component.id} className="stand-create-component stand-create-source">
        <summary>{component.id}<span className="sub">Технический компонент, необходимый для работы стенда.</span></summary>
        <p className="sub">Репозиторий и ревизия заданы шаблоном.</p>
        <dl className="stand-create-summary">
          <dt>Репозиторий</dt><dd>{repository ? `${repository.title} (${repository.key})` : component.repository}</dd>
          <dt>Ветка, тег или SHA</dt><dd><code>{component.default_ref}</code></dd>
        </dl>
        {(!repository?.enabled || !repository.components.includes(component.id)) && adminHelp(`Репозиторий «${component.repository}» недоступен для компонента «${component.id}».`)}
      </details>;
    })}
  </>;
}

function RefInput({ host, repository, component, value, loaded, onChange }: { host: string; repository: string; component: string; value: string; loaded?: RepositoryRefs; onChange: (value: string) => void }) {
  const listId = `stand-refs-${encodeURIComponent(component)}`;
  return <div className="stand-create-ref"><label>Ветка, тег или SHA для {component}<input aria-label={`Ветка, тег или SHA для ${component}`} value={value} onChange={(event) => onChange(event.target.value)} disabled={!host || !repository} list={listId} autoComplete="off" placeholder="refs/heads/main или SHA" aria-describedby={`${listId}-help`} /></label>
    <datalist id={listId}>{loaded?.refs.map((ref) => { const full = `refs/${ref.kind === "branch" ? "heads" : "tags"}/${ref.name}`; return <option key={full} value={full}>{ref.kind === "branch" ? "Ветка" : "Тег"}: {ref.name}</option>; })}</datalist>
    <p className="sub" id={`${listId}-help`}>{!host ? "Сначала выберите хост." : !repository ? "Сначала выберите репозиторий." : "Можно выбрать локальную ветку или тег из подсказок либо ввести ref или SHA вручную."}</p>
    {host && repository && (!loaded || loaded.loading) && <p className="sub" role="status">Загрузка веток и тегов…</p>}
  </div>;
}
