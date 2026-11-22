import { useState, type FormEvent } from "react";
import { sendJson } from "./api";
import { CatalogDialog } from "./catalog-dialog";
import type { GitServer } from "./stand-api";

const providerTitles = { gitlab: "GitLab", github: "GitHub", other: "HTTPS Git" };

function accessRights(server: GitServer): string {
  if (!server.has_token || server.access !== "api") return "—";
  if (server.provider === "gitlab") return "read_api";
  if (server.provider === "github") return "Metadata: read";
  return "—";
}

function GitTokenHelp({ provider, access }: { provider: GitServer["provider"]; access: GitServer["access"] }) {
  return <p className="sub">{access === "git" ? "Для ручного добавления репозитория токен портала не требуется."
    : provider === "gitlab" ? "GitLab: для списка используйте Personal, Project или Group Access Token с правом read_api."
      : "GitHub: для списка используйте fine-grained Personal Access Token для выбранных репозиториев с правом Metadata: read."}</p>;
}

export function GitServers({ servers, loading, loadError, onSaved }: { servers: GitServer[]; loading: boolean; loadError: string; onSaved: () => void }) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<GitServer | null>(null);
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const [provider, setProvider] = useState<GitServer["provider"]>("gitlab");
  const [access, setAccess] = useState<GitServer["access"]>("git");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  function choose(server: GitServer | null) {
    setSelected(server); setTitle(server?.title ?? ""); setUrl(server?.base_url ?? "");
    setProvider(server?.provider ?? "gitlab"); setAccess(server?.access ?? "git");
    setToken(""); setError(""); setNotice("");
  }
  function openEditor(server: GitServer | null) {
    choose(server);
    setOpen(true);
  }
  function closeEditor() {
    setToken("");
    setOpen(false);
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (busy) return;
    setBusy(true); setError(""); setNotice(""); setToken("");
    try {
      const server = selected
        ? await sendJson<GitServer>(`/api/git-servers/${encodeURIComponent(selected.id)}`, "PATCH", { title })
        : await sendJson<GitServer>("/api/git-servers", "POST", { title, base_url: url, provider });
      choose(server); setNotice("Git-сервер сохранён. Для выбора репозиториев из списка настройте токен."); onSaved();
    } catch { setError("Не удалось сохранить Git-сервер. Проверьте название и HTTPS адрес без учётных данных."); }
    finally { setBusy(false); setToken(""); }
  }
  async function saveToken(remove = false) {
    if (!selected || busy || (!remove && !token.trim())) return;
    const value = remove ? null : token;
    setToken(""); setBusy(true); setError(""); setNotice("");
    try {
      const server = await sendJson<GitServer>(`/api/git-servers/${encodeURIComponent(selected.id)}/token`, "PUT", { token: value, access });
      choose(server); setNotice(remove ? "Токен удалён." : "Токен сохранён."); onSaved();
    } catch { setError("Не удалось обновить токен. Проверьте права и параметры подключения, затем введите токен снова."); }
    finally { setBusy(false); setToken(""); }
  }
  return <section aria-labelledby="catalog-git-servers">
    <div className="stand-catalog-section-head">
      <h2 id="catalog-git-servers">Git-серверы</h2>
      <button type="button" className="primary" onClick={() => openEditor(null)}>Добавить сервер</button>
    </div>
    {loading && !servers.length && <p role="status">Загрузка Git-серверов…</p>}
    {loadError && <p className="message error" role="alert">Не удалось загрузить Git-серверы. Обновите каталог.</p>}
    {!loading && !loadError && !servers.length && <p className="sub">Git-серверы пока не добавлены.</p>}
    {servers.length > 0 && <div className="stand-catalog-table-wrap">
      <table className="stand-catalog-server-table" aria-labelledby="catalog-git-servers">
        <thead><tr><th scope="col">Сервер</th><th scope="col">Тип</th><th scope="col">Токен</th><th scope="col">Права для режима</th><th scope="col" aria-label="Действия" /></tr></thead>
        <tbody>{servers.map((server) => <tr key={server.id}>
          <th scope="row"><strong>{server.title}</strong><div className="sub">{server.base_url}</div></th>
          <td>{providerTitles[server.provider]}</td>
          <td><span className={`tag ${server.has_token ? "good" : "muted"}`}>{server.has_token ? "Сохранён" : "Не задан"}</span></td>
          <td>{accessRights(server)}</td>
          <td><button type="button" className="secondary" onClick={() => openEditor(server)} aria-label={`Изменить Git-сервер ${server.title}`}>Изменить</button></td>
        </tr>)}</tbody>
      </table>
    </div>}
    {open && <CatalogDialog title={selected ? "Настройка Git-сервера" : "Добавить Git-сервер"} busy={busy} onClose={closeEditor}>
      <form className="form" onSubmit={save}>
        <fieldset className="stand-catalog-fields" disabled={busy}>
          <label>Название Git-сервера<input required maxLength={200} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
          <label>Адрес Git-сервера<input type="url" required readOnly={Boolean(selected)} placeholder="https://git.example.org" value={url} onChange={(event) => setUrl(event.target.value)} /></label>
          <label>Тип Git-сервера<select disabled={Boolean(selected)} value={provider} onChange={(event) => { setProvider(event.target.value as GitServer["provider"]); setAccess("git"); setToken(""); }}><option value="gitlab">GitLab</option><option value="github">GitHub</option><option value="other">Другой HTTPS Git-сервер</option></select></label>
          {selected && <span className="sub">Адрес и тип неизменяемы. Для другого адреса создайте новое подключение.</span>}
        </fieldset>
        <div><button className="primary" disabled={busy}>Сохранить Git-сервер</button></div>
      </form>
      {selected && <div className="form">
        <h4>Доступ к Git-серверу · {selected.has_token ? "Токен сохранён" : "Токен не задан"}</h4>
        <fieldset className="stand-catalog-fields" disabled={busy}>
          <label>Режим доступа<select value={access} onChange={(event) => setAccess(event.target.value as GitServer["access"])}><option value="git">Ручное добавление</option>{provider !== "other" && <option value="api">Список репозиториев</option>}</select></label>
          <GitTokenHelp provider={provider} access={access} />
          <label>Новый токен<input type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} /><span className="sub">Сохранённое значение не показывается. Для замены введите новый токен.</span></label>
          <div className="links"><button type="button" className="secondary" disabled={!token.trim()} onClick={() => void saveToken()}>Сохранить токен</button><button type="button" className="secondary" disabled={!selected.has_token} onClick={() => void saveToken(true)}>Удалить токен</button></div>
        </fieldset>
      </div>}
      {error && <p className="message error" role="alert">{error}</p>}{notice && <p className="stand-catalog-success" role="status">{notice}</p>}
    </CatalogDialog>}
  </section>;
}
