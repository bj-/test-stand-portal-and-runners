import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, getJson, getText, sendJson, sendRawJson, type DataBinding, type DataJob, type DatasetDetail, type DatasetSummary, type StandTestData } from "./api";
import { formatDate, useLoad } from "./common";

const LIMIT = 2 * 1024 * 1024;
export function validateObject(text: string): string {
  if (new TextEncoder().encode(text).byteLength > LIMIT) throw new Error("Исходный JSON с пробелами превышает 2 МиБ.");
  let value: unknown;
  try {
    // Parse only to validate; never serialize the parsed value (int64 would lose precision).
    value = JSON.parse(text, (_key, item: unknown) => {
      if (typeof item === "number" && !Number.isFinite(item)) throw new Error("non-finite");
      return item;
    });
  } catch { throw new Error("Некорректный JSON: проверьте синтаксис и отсутствие бесконечных чисел."); }
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("В корне JSON должен быть объект.");
  return text;
}
const errorText = (error: unknown) => error instanceof Error ? error.message : "Не удалось выполнить действие.";
const STATUS: Record<string, string> = { queued: "В очереди", running: "Выполняется", done: "Завершена", failed: "Ошибка", cancelled: "Отменена" };

// Keep existing rows mounted while refreshing, so unsaved binding fields survive.
function useRefresh<T>(load: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  useEffect(() => {
    let active = true;
    setError(null);
    load().then((value) => { if (active) { setData(value); setForbidden(false); } }).catch((e: unknown) => { if (active) { setError(errorText(e)); setForbidden(e instanceof ApiError && e.status === 403); } });
    return () => { active = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, forbidden };
}

/** Forms are deliberately independent of refreshed catalog/stand responses. */
export function TestDatasets({ project, stand }: { project: string; stand: string }) {
  const base = `/api/projects/${encodeURIComponent(project)}`;
  const standBase = `${base}/stands/${encodeURIComponent(stand)}/test-data`;
  const [reload, setReload] = useState(0);
  const catalog = useRefresh(() => getJson<DatasetSummary[]>(`${base}/datasets`), [base, reload]);
  const config = useRefresh(() => getJson<StandTestData>(standBase), [standBase, reload]);
  const [selected, setSelected] = useState(0);
  const [version, setVersion] = useState(0);
  const detail = useLoad(() => selected ? getJson<DatasetDetail>(`${base}/datasets/${selected}`) : Promise.resolve(null), [base, selected, reload]);
  const content = useLoad(() => selected && version ? getText(`${base}/datasets/${selected}/versions/${version}/content`) : Promise.resolve(null), [base, selected, version]);
  const [target, setTarget] = useState("");
  const [json, setJson] = useState("{}" );
  const [key, setKey] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [format, setFormat] = useState("");
  const [query, setQuery] = useState("{}");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);
  const [poll, setPoll] = useState(0);
  const jobState = useLoad(() => job ? getJson<DataJob>(`${base}/test-data/jobs/${job.id}`) : Promise.resolve(null), [base, job?.id, poll]);
  const [resultReload, setResultReload] = useState(0);
  const terminal = jobState.data?.status === "done" || jobState.data?.status === "failed";
  const result = useLoad(async () => {
    if (!job || !terminal) return null;
    try { return await getText(`${base}/test-data/jobs/${job.id}/result`); }
    catch (e) { if (e instanceof ApiError && e.status === 409) return null; throw e; }
  }, [base, job?.id, terminal, resultReload]);
  useEffect(() => {
    if (!job || jobState.error || !jobState.data || !["queued", "running"].includes(jobState.data.status)) return;
    const timer = window.setTimeout(() => setPoll((n) => n + 1), 2000);
    return () => window.clearTimeout(timer);
  }, [job, jobState.data, jobState.error, poll]);

  const dataset = catalog.data?.find((item) => item.id === selected);
  const chosenTarget = config.data?.targets.find((item) => item.name === target);
  const compatible = !!dataset && chosenTarget?.format === dataset.format;
  const ready = !!dataset && version > 0 && !!content.data;
  const perform = async (action: () => Promise<void>) => {
    setBusy(true); setError(""); setNotice("");
    try { await action(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const queue = async (operation: "apply" | "inspect") => {
    const queued = operation === "apply"
      ? await sendJson<{ id: number }>(`${standBase}/apply`, "POST", { dataset_id: selected, version, target })
      : await sendRawJson<{ id: number }>(`${standBase}/inspect`, "POST", `{"target":${JSON.stringify(target)},"query":${validateObject(query)}}`);
    setJob({ id: queued.id, label: operation === "apply" ? `Применение «${dataset?.title}», версия ${version}` : `Просмотр: ${chosenTarget?.title}` });
    setNotice("Задача поставлена в очередь.");
  };
  const download = () => {
    if (!content.data || !dataset) return;
    const url = URL.createObjectURL(new Blob([content.data], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `${dataset.key}-v${version}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };

  if (catalog.forbidden || config.forbidden) return <p className="sub">Наборы проекта доступны разработчикам, тестировщикам и администраторам.</p>;

  return <section className="test-datasets">
    <div className="actions"><h3>Наборы проекта</h3><button className="link" disabled={busy} onClick={() => setReload((n) => n + 1)}>Обновить каталог</button></div>
    <p className="sub">Общие наборы проекта с автором и неизменяемыми версиями. Сохранение не меняет данные стенда.</p>
    {catalog.error && <p role="alert" className="message error">Каталог недоступен: {catalog.error}</p>}
    {!catalog.data && !catalog.error && <p className="sub">Загрузка каталога…</p>}
    {catalog.data?.length === 0 && <p className="sub">Наборов пока нет. Создайте первый в редакторе ниже.</p>}
    <div className="data-fields">
      <label>Набор<select disabled={busy || !catalog.data} value={selected} onChange={(e) => { setSelected(Number(e.target.value)); setVersion(0); }}>
        <option value={0}>Выберите набор</option>
        {catalog.data?.map((item) => <option value={item.id} key={item.id}>{item.title} · {item.created_by} · {item.format}</option>)}
      </select></label>
      <label>Версия<select value={version} disabled={busy || !detail.data} onChange={(e) => setVersion(Number(e.target.value))}>
        <option value={0}>Выберите версию</option>
        {detail.data?.versions.map((item) => <option value={item.version} key={item.version}>v{item.version} · {item.created_by} · {formatDate(item.created)}</option>)}
      </select></label>
    </div>
    {selected > 0 && !detail.data && !detail.error && <p className="sub">Загрузка версий…</p>}
    {detail.error && <p role="alert" className="message error">Версии недоступны: {detail.error}</p>}
    {dataset && <p className="sub">{dataset.description} Автор: {dataset.created_by}. Последняя версия: {dataset.version}.</p>}
    {version > 0 && !content.data && !content.error && <p className="sub">Загрузка JSON…</p>}
    {content.error && <p role="alert" className="message error">JSON недоступен: {content.error}</p>}
    {content.data && <details><summary>JSON версии {version}</summary><pre className="data-json">{content.data}</pre>
      <div className="actions"><button className="secondary" onClick={download}>Скачать JSON</button><button className="secondary" disabled={busy} onClick={() => { setJson(content.data!); setNotice("Версия скопирована в редактор. Изменения пока не сохранены."); }}>Скопировать в редактор</button></div>
    </details>}

    <details className="data-editor"><summary>Создать набор или новую версию</summary>
      <div className="data-fields">
        <label>Ключ нового набора<input value={key} onChange={(e) => setKey(e.target.value)} disabled={busy} placeholder="orders-smoke" /></label>
        <label>Название нового набора<input value={title} onChange={(e) => setTitle(e.target.value)} disabled={busy} /></label>
        <label>Формат нового набора<input list="test-data-formats" value={format} onChange={(e) => setFormat(e.target.value)} disabled={busy} placeholder="relational-v1" /></label>
        <datalist id="test-data-formats">{Array.from(new Set(config.data?.targets.map((item) => item.format) ?? [])).map((item) => <option key={item} value={item} />)}</datalist>
        <label>Описание нового набора<input value={description} onChange={(e) => setDescription(e.target.value)} disabled={busy} /></label>
      </div>
      <label className="data-label">JSON-файл (до 2 МиБ с учётом пробелов)<input type="file" accept=".json,application/json" disabled={busy} onChange={(e) => {
        const file = e.target.files?.[0]; e.target.value = "";
        if (!file) return;
        if (file.size > LIMIT) { setError("Файл превышает 2 МиБ. Редактор не изменён."); return; }
        void perform(async () => { const text = await file.text(); validateObject(text); setJson(text); setNotice("JSON загружен в редактор. Сохраните набор или новую версию."); });
      }} /></label>
      <label className="data-label">JSON данных<textarea className="data-json" rows={10} spellCheck={false} disabled={busy} value={json} onChange={(e) => setJson(e.target.value)} /></label>
      <p className="sub">Лимит редактора и запроса — 2 МиБ исходного текста в UTF-8, включая пробелы. Числа передаются без изменения.</p>
      <div className="actions">
        <button className="secondary" disabled={busy || !key.trim() || !title.trim() || !format.trim()} onClick={() => void perform(async () => {
          const metadata = JSON.stringify({ key: key.trim(), title: title.trim(), description, format: format.trim() });
          const saved = await sendRawJson<DatasetSummary>(`${base}/datasets`, "POST", `${metadata.slice(0, -1)},"data":${validateObject(json)}}`);
          setReload((n) => n + 1); setSelected(saved.id); setVersion(0); setNotice(`Набор «${saved.title}» сохранён. Выберите версию для применения.`);
        })}>Сохранить новый набор</button>
        <button className="secondary" disabled={busy || !dataset?.can_edit} onClick={() => void perform(async () => {
          const saved = await sendRawJson<{ version: number }>(`${base}/datasets/${selected}/versions`, "POST", `{"data":${validateObject(json)}}`);
          setReload((n) => n + 1); setNotice(`Сохранена версия ${saved.version} набора «${dataset?.title}». Выбранная версия и привязки не изменены.`);
        })}>Сохранить новую версию{dataset ? ` «${dataset.title}»` : ""}</button>
      </div>
      <p className="sub">Новая версия сохраняется в выбранный набор с его прежним форматом. Разработчик может добавлять версии только своих наборов.</p>
    </details>

    <h3>Данные стенда</h3>
    {config.error && <p role="alert" className="message error">Настройки стенда недоступны: {config.error}</p>}
    {!config.data && !config.error && <p className="sub">Загрузка целей и подключений…</p>}
    {config.data?.targets.length === 0 && <p className="sub">Цели для наборов не настроены. Каталог доступен; для применения и просмотра добавьте цель в конфигурацию стенда.</p>}
    <label className="data-label">Цель<select disabled={busy || !config.data} value={target} onChange={(e) => setTarget(e.target.value)}>
      <option value="">Выберите цель</option>{config.data?.targets.map((item) => <option key={item.name} value={item.name}>{item.title} · {item.format}</option>)}
    </select></label>
    {dataset && chosenTarget && !compatible && <p className="message">Формат набора {dataset.format} не подходит цели {chosenTarget.format}.</p>}
    <div className="actions">
      <button className="primary" disabled={busy || !ready || !compatible || !chosenTarget?.can_apply} onClick={() => void perform(() => queue("apply"))}>Залить выбранную версию сейчас</button>
      {config.data?.can_manage && <button className="secondary" disabled={busy || !ready || !compatible || !chosenTarget?.can_apply} onClick={() => void perform(async () => {
        await sendJson(`${standBase}/bindings`, "POST", { dataset_id: selected, version, target, enabled: true, auto_apply: true, position: 0 });
        setReload((n) => n + 1); setNotice("Версия подключена. Данные сейчас не изменены.");
      })}>Подключить выбранную версию к стенду</button>}
    </div>
    <p className="sub">«Залить» меняет данные выбранной цели по правилам её формата. Подключение применяется при следующем развёртывании, засеве или сбросе, если включено автоприменение.</p>
    {chosenTarget && !chosenTarget.can_apply && <p className="sub">Для этой цели применение не настроено.</p>}
    <h3>Подключённые наборы</h3>
    {config.data?.bindings.length === 0 && <p className="sub">К стенду пока не подключены наборы.</p>}
    {config.data && !config.data.can_manage && <p className="sub">Подключениями управляют тестировщик и администратор.</p>}
    {config.data?.bindings.map((binding) => <BindingEditor key={binding.id} binding={binding} base={base} standBase={standBase} revision={reload} canManage={config.data!.can_manage} onChanged={() => setReload((n) => n + 1)} />)}
    <details><summary>Посмотреть фактические данные</summary>
      <label className="data-label">Запрос JSON (до 2 МиБ с учётом пробелов)<textarea className="data-json" rows={5} disabled={busy} spellCheck={false} value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      <button className="secondary" disabled={busy || !chosenTarget?.can_inspect} onClick={() => void perform(() => queue("inspect"))}>Запросить данные выбранной цели</button>
      {chosenTarget && !chosenTarget.can_inspect && <p className="sub">Для этой цели просмотр не настроен.</p>}
    </details>
    {busy && <p role="status" className="sub">Отправляю…</p>}
    {error && <p role="alert" className="message error">{error}</p>}
    {notice && <p role="status" className="message">{notice}</p>}
    {job && <section aria-label="Последняя задача данных"><Link to={`/jobs/${job.id}`}>Задача #{job.id}</Link> · {job.label}
      <p role="status">{jobState.data ? STATUS[jobState.data.status] ?? jobState.data.status : jobState.error ? "Обновление остановлено" : "Получаю состояние…"}{jobState.data?.message ? `: ${jobState.data.message}` : ""}</p>
      {jobState.error && <p role="alert" className="message error">Не удалось получить результат: {jobState.error} <button className="link" onClick={() => setPoll((n) => n + 1)}>Повторить</button></p>}
      {terminal && result.error && <p role="alert" className="message error">Не удалось получить JSON: {result.error} <button className="link" onClick={() => setResultReload((n) => n + 1)}>Повторить</button></p>}
      {terminal && !result.data && !result.error && <p className="sub">Результат пока недоступен. <button className="link" onClick={() => setResultReload((n) => n + 1)}>Обновить результат</button></p>}
      {terminal && result.data && <details open><summary>Результат JSON</summary><pre className="data-json">{result.data}</pre></details>}
    </section>}
  </section>;
}

function BindingEditor({ binding, base, standBase, revision, canManage, onChanged }: { binding: DataBinding; base: string; standBase: string; revision: number; canManage: boolean; onChanged: () => void }) {
  const [draft, setDraft] = useState(binding);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [detach, setDetach] = useState(false);
  const versions = useLoad(() => getJson<DatasetDetail>(`${base}/datasets/${binding.dataset_id}`), [base, binding.dataset_id, revision]);
  const save = async (remove: boolean) => {
    setBusy(true); setError(""); setNotice("");
    try {
      if (!remove && !Number.isInteger(draft.position)) throw new Error("Порядок должен быть целым числом.");
      await sendJson(`${standBase}/bindings/${binding.id}`, remove ? "DELETE" : "PUT", remove ? undefined : { dataset_id: draft.dataset_id, version: draft.version, target: draft.target, enabled: draft.enabled, auto_apply: draft.auto_apply, position: draft.position });
      setDetach(false); setNotice("Подключение сохранено. Данные сейчас не изменены."); onChanged();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return <fieldset className="data-binding" disabled={!canManage || busy}>
    <legend>{binding.title} · {binding.target}</legend>
    <div className="data-fields">
      <label>Версия<select value={draft.version} onChange={(e) => setDraft({ ...draft, version: Number(e.target.value) })}>
        {!versions.data?.versions.some((item) => item.version === draft.version) && <option value={draft.version}>v{draft.version}</option>}
        {versions.data?.versions.map((item) => <option key={item.version} value={item.version}>v{item.version}</option>)}
      </select></label>
      <label>Порядок<input type="number" step={1} value={Number.isNaN(draft.position) ? "" : draft.position} onChange={(e) => setDraft({ ...draft, position: e.target.valueAsNumber })} /></label>
      <label className="check"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />Включено</label>
      <label className="check"><input type="checkbox" checked={draft.auto_apply} onChange={(e) => setDraft({ ...draft, auto_apply: e.target.checked })} />Автоприменение</label>
    </div>
    {versions.error && <p className="message error">Список версий недоступен: {versions.error}</p>}
    {canManage && <div className="actions"><button className="secondary" onClick={() => void save(false)}>Сохранить подключение</button><button className="link" onClick={() => setDetach(!detach)}>Отсоединить…</button></div>}
    {detach && <div><p>Отсоединить «{binding.title}»? Данные в базе останутся.</p><div className="actions"><button className="secondary danger" onClick={() => void save(true)}>Отсоединить набор</button><button className="link" onClick={() => setDetach(false)}>Отмена</button></div></div>}
    {error && <p role="alert" className="message error">{error}</p>}{notice && <p role="status" className="sub">{notice}</p>}
  </fieldset>;
}
