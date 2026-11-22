import { useEffect, useState, type FormEvent } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { getJson, sendJson, type Me, type PortalUser } from "./api";
import { Loaded, formatDate, useLoad } from "./common";
import { ConfirmDialog, applyTheme, type Consequences } from "./confirm";

export const ROLES = [
  ["admin", "Администратор"],
  ["tester", "Тестировщик"],
  ["developer", "Разработчик"],
  ["viewer", "Только просмотр"],
] as const;
export const roleTitle = (role: string) => ROLES.find(([value]) => value === role)?.[1] ?? role;

export function LoginPage({ onDone }: { onDone: (me: Me) => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    sendJson<Me>("/api/auth/login", "POST", { login, password })
      .then(onDone)
      .catch((e: Error) => setError(e.message));
  };
  return (
    <form className="card narrow" onSubmit={submit}>
      <h1>Test Portal</h1>
      <label>Логин<input autoFocus autoComplete="username" value={login} onChange={(e) => setLogin(e.target.value)} required /></label>
      <label>Пароль<input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
      {error && <p className="message error">{error}</p>}
      <button className="primary">Войти</button>
    </form>
  );
}

export function ChangePassword({ onDone, forced, embedded }: { onDone: () => void; forced?: boolean; embedded?: boolean }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [state, setState] = useState<{ error?: string; done?: boolean }>({});
  const submit = (event: FormEvent) => {
    event.preventDefault();
    sendJson("/api/auth/password", "POST", { current, new: next })
      .then(() => {
        setState({ done: true });
        setCurrent("");
        setNext("");
        onDone();
      })
      .catch((e: Error) => setState({ error: e.message }));
  };
  return (
    <form className={embedded ? "form" : "card narrow"} onSubmit={submit}>
      {embedded ? <h2>Смена пароля</h2> : <h1>Смена пароля</h1>}
      {forced && <p>Задайте свой пароль: разовый выдан администратором.</p>}
      <label>Текущий пароль<input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required /></label>
      <label>Новый пароль, от 10 символов<input type="password" autoComplete="new-password" minLength={10} value={next} onChange={(e) => setNext(e.target.value)} required /></label>
      {state.error && <p className="message error">{state.error}</p>}
      {state.done && <p className="message">Пароль изменён</p>}
      <div className="actions"><button className="primary">Сменить пароль</button></div>
    </form>
  );
}

export function PasswordSuggestion({ onDismiss }: { onDismiss: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dismiss = () => {
    setPending(true);
    setError(null);
    sendJson("/api/auth/password-suggestion/dismiss", "POST")
      .then(onDismiss)
      .catch((e: Error) => setError(e.message))
      .finally(() => setPending(false));
  };
  return (
    <aside className="notice" aria-label="Начальный пароль">
      <p>Вы вошли с начальным паролем. Можно сменить его сейчас или оставить как есть.
        Позже пароль всегда можно сменить в профиле.</p>
      <div className="actions">
        <Link to="/profile#password">Сменить пароль</Link>
        <button className="secondary" disabled={pending} onClick={dismiss}>{pending ? "Сохранение…" : "Оставить как есть"}</button>
      </div>
      {error && <p className="message error" role="alert">Не удалось сохранить выбор: {error}. Попробуйте ещё раз.</p>}
    </aside>
  );
}

type Contacts = { name: string; email: string; telegram: string };
const contacts = (user: PortalUser): Contacts => ({ name: user.name, email: user.email ?? "", telegram: user.telegram ?? "" });

/** Имя, e-mail и Telegram - общая часть профиля и карточки пользователя. */
function ContactFields({ value, onChange }: { value: Contacts; onChange: (value: Contacts) => void }) {
  return (
    <>
      <label>Имя<input value={value.name} maxLength={200} onChange={(e) => onChange({ ...value, name: e.target.value })} /></label>
      <label>E-mail<input type="email" value={value.email} onChange={(e) => onChange({ ...value, email: e.target.value })} /></label>
      <label>Telegram<input value={value.telegram} placeholder="@имя" onChange={(e) => onChange({ ...value, telegram: e.target.value })} /></label>
    </>
  );
}

export function ProfilePage({ onPasswordChanged }: { onPasswordChanged: () => void }) {
  const location = useLocation();
  const [version, setVersion] = useState(0);
  const state = useLoad(() => getJson<PortalUser>("/api/profile"), [version]);
  const [form, setForm] = useState<Contacts | null>(null);
  const [message, setMessage] = useState<{ error?: string; done?: boolean }>({});
  useEffect(() => {
    if (state.data) setForm(contacts(state.data));
  }, [state.data]);
  useEffect(() => {
    if (state.data && location.hash === "#password") {
      const section = document.getElementById("password");
      section?.scrollIntoView();
      section?.querySelector("input")?.focus({ preventScroll: true });
    }
  }, [state.data, location.hash]);

  const save = (event: FormEvent) => {
    event.preventDefault();
    sendJson<PortalUser>("/api/profile", "PATCH", form)
      .then(() => {
        setMessage({ done: true });
        setVersion((v) => v + 1);
      })
      .catch((e: Error) => setMessage({ error: e.message }));
  };

  return (
    <>
      <header className="page-head"><h1>Профиль</h1></header>
      <Loaded state={state}>
        {(user) => (
          <div className="profile">
            <section className="gpanel">
              <header><h2>{user.login}</h2><span className="tag muted">{roleTitle(user.role)}</span></header>
              <div className="gpanel-body">
                <p className="sub">Последний вход: {formatDate(user.last_login ?? null)}. Роль меняет администратор.</p>
                {form && (
                  <form className="form" onSubmit={save}>
                    <ContactFields value={form} onChange={(value) => { setForm(value); setMessage({}); }} />
                    {message.error && <p className="message error">{message.error}</p>}
                    {message.done && <p className="message">Сохранено</p>}
                    <div className="actions"><button className="primary">Сохранить</button></div>
                  </form>
                )}
              </div>
            </section>
            <section className="gpanel">
              <header><h2>Оформление</h2></header>
              <div className="gpanel-body">
                <ThemeChoice value={user.theme || "dark"} />
              </div>
            </section>
            <section className="gpanel" id="password">
              <div className="gpanel-body"><ChangePassword onDone={onPasswordChanged} embedded /></div>
            </section>
          </div>
        )}
      </Loaded>
    </>
  );
}

const THEMES = [
  ["dark", "Тёмная", "по умолчанию"],
  ["light", "Светлая", ""],
  ["system", "Как в системе", "светлая или тёмная - по настройке компьютера"],
] as const;

/** Тема применяется сразу и сохраняется в профиле: действует на любом компьютере. */
function ThemeChoice({ value }: { value: string }) {
  const [theme, setTheme] = useState(value);
  const [error, setError] = useState<string | null>(null);
  const choose = (next: string) => {
    setTheme(next);
    applyTheme(next);
    sendJson("/api/profile", "PATCH", { theme: next }).then(() => setError(null)).catch((e: Error) => setError(e.message));
  };
  return (
    <div className="theme-choice" role="radiogroup" aria-label="Тема оформления">
      {THEMES.map(([id, title, note]) => (
        <label key={id} className={`theme-option ${theme === id ? "selected" : ""}`}>
          <input type="radio" name="theme" value={id} checked={theme === id} onChange={() => choose(id)} />
          <span className={`theme-preview ${id}`} aria-hidden="true"><i /><i /><i /></span>
          <span><b>{title}</b>{note && <span className="sub"> - {note}</span>}</span>
        </label>
      ))}
      {error && <p className="message error">{error}</p>}
    </div>
  );
}

const USER_ACTIONS: Record<string, Consequences> = {
  reset_password: {
    what: ["Выдать пользователю новый разовый пароль (показывается один раз)", "Завершить все его сеансы", "Снять блокировку входа"],
    risks: ["Старый пароль перестанет работать сразу", "Пользователь не войдёт, пока вы не передадите ему новый пароль"],
  },
  disable: {
    what: ["Запретить пользователю вход", "Завершить все его сеансы"],
    risks: ["Пользователь потеряет доступ к порталу до включения", "Его задачи и данные сохраняются"],
  },
};

function Issued({ user }: { user: PortalUser }) {
  return (
    <p className="notice">
      Разовый пароль для <b>{user.login}</b>: <code>{user.password}</code>. Он показан один раз, при входе пользователь задаст свой.
    </p>
  );
}

function UserState({ user }: { user: PortalUser }) {
  if (!user.active) return <span className="tag muted">– отключён</span>;
  if (user.locked) return <span className="tag warning">! вход заблокирован</span>;
  return <span className="tag good">✓ активен</span>;
}

export function UsersPage() {
  const [version, setVersion] = useState(0);
  const state = useLoad(() => getJson<PortalUser[]>("/api/users"), [version]);
  const [issued, setIssued] = useState<PortalUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState({ login: "", name: "", email: "", role: "viewer" });

  const add = (event: FormEvent) => {
    event.preventDefault();
    sendJson<PortalUser>("/api/users", "POST", draft)
      .then((user) => {
        setError(null);
        setIssued(user);
        setVersion((v) => v + 1);
        setDraft({ login: "", name: "", email: "", role: "viewer" });
      })
      .catch((e: Error) => setError(e.message));
  };

  return (
    <>
      <header className="page-head"><h1>Пользователи</h1></header>
      {issued && <Issued user={issued} />}
      {error && <p className="message error">{error}</p>}
      <form className="inline-form" onSubmit={add}>
        <input aria-label="Логин" placeholder="Логин" value={draft.login} onChange={(e) => setDraft({ ...draft, login: e.target.value })} required />
        <input aria-label="Имя" placeholder="Имя" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
        <input aria-label="E-mail" type="email" placeholder="E-mail" value={draft.email} onChange={(e) => setDraft({ ...draft, email: e.target.value })} />
        <select aria-label="Роль" value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
          {ROLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button className="primary">Добавить</button>
      </form>
      <Loaded state={state}>
        {(users) => (
          <div className="table-wrap">
            <table>
              <thead><tr><th>Логин</th><th>Имя</th><th>E-mail</th><th>Роль</th><th>Состояние</th><th>Последний вход</th></tr></thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.login}>
                    <td><Link to={`/users/${encodeURIComponent(user.login)}`}>{user.login}</Link></td>
                    <td>{user.name || "-"}</td>
                    <td>{user.email || "-"}</td>
                    <td>{roleTitle(user.role)}</td>
                    <td className="nowrap"><UserState user={user} /></td>
                    <td className="nowrap">{formatDate(user.last_login ?? null)}</td>
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

export function UserCardPage({ me }: { me: string }) {
  const { login = "" } = useParams();
  const [version, setVersion] = useState(0);
  const state = useLoad(() => getJson<PortalUser>(`/api/users/${encodeURIComponent(login)}`), [login, version]);
  const [form, setForm] = useState<(Contacts & { role: string }) | null>(null);
  const [issued, setIssued] = useState<PortalUser | null>(null);
  const [message, setMessage] = useState<{ error?: string; done?: string }>({});
  const [asking, setAsking] = useState<"reset_password" | "disable" | null>(null);
  useEffect(() => {
    if (state.data) setForm({ ...contacts(state.data), role: state.data.role });
  }, [state.data]);

  const patch = (body: object, done: string) =>
    sendJson<PortalUser>(`/api/users/${encodeURIComponent(login)}`, "PATCH", body)
      .then((user) => {
        setMessage({ done });
        setIssued(user.password ? user : null);
        setVersion((v) => v + 1);
      })
      .catch((e: Error) => setMessage({ error: e.message }));
  const save = (event: FormEvent) => {
    event.preventDefault();
    patch(form ?? {}, "Сохранено");
  };
  const self = login === me;

  return (
    <>
      <p className="links"><Link to="/users">← Пользователи</Link></p>
      <Loaded state={state}>
        {(user) => (
          <>
            <header className="page-head">
              <h1>{user.login}</h1>
              <UserState user={user} />
            </header>
            {issued && <Issued user={issued} />}
            <div className="profile">
              <section className="gpanel">
                <header><h2>Данные и роль</h2></header>
                <div className="gpanel-body">
                  <p className="sub">Последний вход: {formatDate(user.last_login ?? null)}</p>
                  {form && (
                    <form className="form" onSubmit={save}>
                      <ContactFields value={form} onChange={(value) => { setForm({ ...form, ...value }); setMessage({}); }} />
                      <label>Роль
                        <select value={form.role} disabled={self} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                          {ROLES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                        </select>
                        {self && <span className="sub">Свою роль администратор не меняет: портал останется без администратора</span>}
                      </label>
                      <div className="actions"><button className="primary">Сохранить</button></div>
                    </form>
                  )}
                </div>
              </section>
              <section className="gpanel">
                <header><h2>Доступ</h2></header>
                <div className="gpanel-body user-actions">
                  <div>
                    <button className="secondary" onClick={() => setAsking("reset_password")}>Сбросить пароль</button>
                    <span className="sub">Выдаёт разовый пароль, завершает сеансы пользователя.</span>
                  </div>
                  {user.locked && (
                    <div>
                      <button className="secondary" onClick={() => patch({ unlock: true }, "Блокировка снята")}>Снять блокировку входа</button>
                      <span className="sub">Вход заблокирован после 5 неудачных попыток; сам снимется через 15 минут.</span>
                    </div>
                  )}
                  {!self && (
                    <div>
                      <button className={user.active ? "secondary danger" : "secondary"}
                        onClick={() => (user.active ? setAsking("disable") : patch({ active: true }, "Включён"))}>
                        {user.active ? "Отключить" : "Включить"}
                      </button>
                      <span className="sub">{user.active ? "Отключённый не может войти, сеансы завершаются." : "Вернуть доступ."}</span>
                    </div>
                  )}
                </div>
              </section>
            </div>
            {message.error && <p className="message error">{message.error}</p>}
            {message.done && <p className="message">{message.done}</p>}
            {asking && (
              <ConfirmDialog
                title={asking === "disable" ? `Отключить ${user.login}` : `Сбросить пароль ${user.login}`}
                info={USER_ACTIONS[asking]}
                danger={asking === "disable"}
                confirmLabel={asking === "disable" ? "Отключить" : "Сбросить пароль"}
                onClose={() => setAsking(null)}
                onConfirm={() =>
                  patch(asking === "disable" ? { active: false } : { reset_password: true }, asking === "disable" ? "Отключён" : "Пароль сброшен")
                    .then(() => setAsking(null))
                }
              />
            )}
          </>
        )}
      </Loaded>
    </>
  );
}
