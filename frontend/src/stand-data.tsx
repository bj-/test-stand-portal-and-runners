import { useState } from "react";
import { getJson, sendJson, sendRawJson, type DatasetSummary } from "./api";
import { Loaded, useLoad } from "./common";
import { newIdempotencyKey, projectApi, type StandDataBundle, type StandDataModule } from "./stand-api";
import { validateObject } from "./test-data";

type DatasetDraft = { key: string; title: string; format: string; json: string };
export type InitialDatasetDraft = DatasetDraft & { id: string; module: string };
type EntryDraft = { id: string; module: string; dataset_id: number; version: number };
const newDataset = (): DatasetDraft => ({ key: "", title: "", format: "relational-v1", json: "" });
const ERROR = "Не удалось сохранить тестовые данные.";

function dataBody({ key, title, format, json }: DatasetDraft, extra = {}) {
  const metadata = JSON.stringify({ ...extra, key: key.trim(), title: title.trim(), format: format.trim() });
  return `${metadata.slice(0, -1)},"data":${validateObject(json)}}`;
}

export function initialDataBody(rows: InitialDatasetDraft[]) {
  return `[${rows.map((row) => dataBody(row, { module: row.module.trim() })).join(",")}]`;
}

function JsonInput({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const [error, setError] = useState("");
  return <>
    <label>Загрузить JSON-файл<input type="file" accept=".json,application/json" onChange={(event) => {
      const file = event.target.files?.[0];
      setError("");
      if (!file) return;
      if (file.size > 2 * 1024 * 1024) { setError("Файл превышает 2 МиБ."); return; }
      void file.text().then((text) => onChange(validateObject(text))).catch((failure: unknown) => setError(failure instanceof Error ? failure.message : ERROR));
    }} /></label>
    {error && <p className="message error" role="alert">{error}</p>}
    <label>Данные модуля (JSON)<textarea className="mono" rows={4} value={value} onChange={(event) => onChange(event.target.value)} placeholder="{}" /></label>
  </>;
}

function DatasetFields({ value, onChange }: { value: DatasetDraft; onChange: (patch: Partial<DatasetDraft>) => void }) {
  return <>
    <label>Ключ набора<input value={value.key} maxLength={120} onChange={(event) => onChange({ key: event.target.value })} /></label>
    <label>Название набора<input value={value.title} maxLength={200} onChange={(event) => onChange({ title: event.target.value })} /></label>
    <label>Формат набора<input value={value.format} maxLength={80} onChange={(event) => onChange({ format: event.target.value })} /></label>
    <JsonInput value={value.json} onChange={(json) => onChange({ json })} />
  </>;
}

export function InitialProjectData({ rows, setRows }: { rows: InitialDatasetDraft[]; setRows: React.Dispatch<React.SetStateAction<InitialDatasetDraft[]>> }) {
  const change = (id: string, patch: Partial<InitialDatasetDraft>) => setRows((current) => current.map((row) => row.id === id ? { ...row, ...patch } : row));
  return <details>
    <summary>Базовый комплект тестовых данных</summary>
    <div className="stand-catalog-fields">
      <p className="sub">Комплект создаётся вместе с проектом. Добавьте данные для модулей сейчас или заполните комплект позже в каталоге стендов.</p>
      {rows.map((row) => <div className="stand-catalog-panel stand-catalog-fields" key={row.id}>
        <label>Модуль шаблона<input maxLength={128} value={row.module} onChange={(event) => change(row.id, { module: event.target.value })} /></label>
        <DatasetFields value={row} onChange={(patch) => change(row.id, patch)} />
        <button type="button" className="secondary" onClick={() => setRows((current) => current.filter((item) => item.id !== row.id))}>Убрать модуль</button>
      </div>)}
      <button type="button" className="secondary" disabled={rows.length >= 64} onClick={() => setRows((current) => [...current, { ...newDataset(), id: newIdempotencyKey(), module: "" }])}>Добавить модуль</button>
    </div>
  </details>;
}

export function ProjectDataBundle({ project, modules }: { project: string; modules: Record<string, StandDataModule> }) {
  const base = projectApi(project);
  const [revision, setRevision] = useState(0);
  const catalog = useLoad(() => Promise.all([getJson<StandDataBundle>(`${base}/stand-data-bundle`), getJson<DatasetSummary[]>(`${base}/datasets`)]), [base, revision]);
  const [draft, setDraft] = useState<{ title: string; entries: EntryDraft[] } | null>(null);
  const [dataset, setDataset] = useState<DatasetDraft>(newDataset);
  const [versionOf, setVersionOf] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const changeEntry = (id: string, patch: Partial<EntryDraft>) => setDraft((current) => current && ({ ...current, entries: current.entries.map((entry) => entry.id === id ? { ...entry, ...patch } : entry) }));
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try { await action(); setRevision((current) => current + 1); }
    catch (failure) { setError(failure instanceof Error ? failure.message : ERROR); }
    finally { setBusy(false); }
  }
  return <section className="stand-catalog-panel" aria-labelledby="project-data-title">
    <h2 id="project-data-title">Тестовые данные стендов</h2>
    {error && <p className="message error" role="alert">{error}</p>}
    {notice && <p className="message" role="status">{notice}</p>}
    <Loaded state={catalog}>{([bundle, datasets]) => <>
      {!draft ? <>
        <h3>{bundle.title}</h3>
        {bundle.entries.length ? <dl className="stand-deployment-list">{bundle.entries.map((entry) => <div key={entry.module}>
          <dt>{modules[entry.module]?.title ?? entry.module}</dt><dd>{entry.title} · версия {entry.version} · {entry.format}</dd>
        </div>)}</dl> : <p className="sub">Комплект пока пуст. Добавьте наборы для модулей шаблона.</p>}
        <button className="secondary" type="button" onClick={() => setDraft({ title: bundle.title, entries: bundle.entries.map((entry) => ({ ...entry, id: newIdempotencyKey() })) })}>Изменить комплект</button>
      </> : <fieldset className="stand-catalog-fields" disabled={busy}>
        <legend>Комплект по умолчанию</legend>
        <label>Название комплекта<input value={draft.title} maxLength={200} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
        <datalist id="stand-data-modules">{Object.entries(modules).map(([name, module]) => <option value={name} key={name}>{module.title}</option>)}</datalist>
        {draft.entries.map((entry) => <div key={entry.id} className="stand-catalog-panel stand-catalog-fields">
          <label>Модуль шаблона<input list="stand-data-modules" maxLength={128} value={entry.module} onChange={(event) => changeEntry(entry.id, { module: event.target.value })} /></label>
          <label>Набор<select value={entry.dataset_id || ""} onChange={(event) => {
            const selected = datasets.find((item) => item.id === Number(event.target.value));
            changeEntry(entry.id, { dataset_id: selected?.id ?? 0, version: selected?.version ?? 1 });
          }}><option value="">Выберите набор</option>{datasets.map((item) => <option value={item.id} key={item.id}>{item.title} · {item.format}</option>)}</select></label>
          <label>Версия набора<input type="number" min={1} max={datasets.find((item) => item.id === entry.dataset_id)?.version} step={1} value={entry.version || ""} onChange={(event) => changeEntry(entry.id, { version: Number(event.target.value) })} /></label>
          <button type="button" className="secondary" onClick={() => setDraft({ ...draft, entries: draft.entries.filter((item) => item.id !== entry.id) })}>Убрать модуль</button>
        </div>)}
        <div className="actions">
          <button className="secondary" type="button" disabled={draft.entries.length >= 64} onClick={() => setDraft({ ...draft, entries: [...draft.entries, { id: newIdempotencyKey(), module: "", dataset_id: 0, version: 1 }] })}>Добавить модуль</button>
          <button className="primary" type="button" onClick={() => void perform(async () => {
            const entries = draft.entries.map(({ module, dataset_id, version }) => ({ module: module.trim(), dataset_id, version }));
            await sendJson(`${base}/stand-data-bundle`, "PUT", { title: draft.title.trim(), entries });
            setDraft(null);
            setNotice("Комплект сохранён.");
          })}>Сохранить комплект</button>
          <button className="secondary" type="button" onClick={() => { setDraft(null); setError(""); }}>Отмена</button>
        </div>
      </fieldset>}
      <details>
        <summary>Создать набор или новую версию</summary>
        <fieldset className="stand-catalog-fields" disabled={busy}>
          <label>Набор для публикации<select value={versionOf} onChange={(event) => setVersionOf(Number(event.target.value))}>
            <option value={0}>Новый набор</option>{datasets.filter((item) => item.can_edit).map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}
          </select></label>
          {versionOf ? <JsonInput value={dataset.json} onChange={(json) => setDataset((current) => ({ ...current, json }))} />
            : <DatasetFields value={dataset} onChange={(patch) => setDataset((current) => ({ ...current, ...patch }))} />}
          <button className="secondary" type="button" onClick={() => void perform(async () => {
            if (versionOf) await sendRawJson(`${base}/datasets/${versionOf}/versions`, "POST", `{"data":${validateObject(dataset.json)}}`);
            else await sendRawJson(`${base}/datasets`, "POST", dataBody(dataset));
            setDataset(newDataset());
            setNotice(versionOf ? "Версия опубликована. Выберите её в комплекте, чтобы применять на новых стендах." : "Набор создан. Его можно добавить в комплект.");
          })}>{versionOf ? "Сохранить новую версию" : "Сохранить новый набор"}</button>
        </fieldset>
      </details>
    </>}</Loaded>
  </section>;
}
