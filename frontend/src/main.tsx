import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, Navigate, NavLink, Route, Routes, useNavigate } from "react-router-dom";
import { ApiError, getJson, sendJson, type Me } from "./api";
import { ChangePassword, LoginPage, PasswordSuggestion, ProfilePage, UserCardPage, UsersPage } from "./access";
import { DashboardPage, HelpPageView } from "./dashboard";
import { HelpCenter } from "./helpcenter";
import { BatchPage, JobPage, JobsPage, StartPage } from "./jobs";
import { SeedPage, StandsPage } from "./stands";
import { StandCreatePage } from "./stand-create";
import { StandCatalogPage, StandProjectCreatePage } from "./stand-catalog";
import { StandDetailPage } from "./stand-detail";
import { applyTheme } from "./confirm";
import { BugPage, BugsPage, RunPage, RunsPage, StatsPage } from "./pages";
import { ProjectSummariesPage, ProjectSummaryPage } from "./project-summaries";
import { UpdatesPage } from "./updates";
import "./style.css";

function App() {
  const [me, setMe] = useState<Me | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const refreshing = useRef(false);
  const navigate = useNavigate();
  const refresh = useCallback(() => {
    if (refreshing.current) return;
    refreshing.current = true;
    setMe(undefined);
    setError(null);
    getJson<Me>("/api/auth/me")
      .then((value) => {
        applyTheme(value.theme);
        setMe(value);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => { refreshing.current = false; });
  }, []);

  useEffect(refresh, [refresh]);
  useEffect(() => {
    const signedOut = () => {
      if (me && !me.is_guest) refresh();
    };
    window.addEventListener("portal:signed-out", signedOut);
    return () => window.removeEventListener("portal:signed-out", signedOut);
  }, [me, refresh]);

  if (me === undefined && error) return <div className="message error" role="alert">
    <p>Не удалось загрузить портал: {error}</p>
    <button onClick={refresh}>Повторить</button>
  </div>;
  if (me === undefined) return <p className="message">Загрузка…</p>;
  if (me.must_change_password) return <ChangePassword onDone={refresh} forced />;

  const canRun = !me.is_guest && ["admin", "tester", "developer"].includes(me.role);
  const canViewTestData = !me.is_guest && me.role !== "viewer";
  const denied = me.is_guest
    ? <p className="message">Для этой страницы нужно <Link to="/login">войти</Link>.</p>
    : <p className="message">Недостаточно прав для этой страницы.</p>;
  const signedIn = (value: Me) => {
    applyTheme(value.theme);
    setMe(value);
    setError(null);
    navigate("/", { replace: true });
  };
  const signOut = () => {
    setError(null);
    sendJson("/api/auth/logout", "POST")
      .then(() => { navigate("/", { replace: true }); refresh(); })
      .catch((e: Error) => {
        if (e instanceof ApiError && e.status === 401) {
          navigate("/", { replace: true });
          refresh();
        } else setError(e.message);
      });
  };
  return (
    <>
      <nav className="top">
        <span className="brand">Test Portal</span>
        <NavLink to="/" end>Обзор</NavLink>
        <NavLink to="/runs">Прогоны</NavLink>
        <NavLink to="/summaries">Сводки</NavLink>
        <NavLink to="/stands">Стенды</NavLink>
        <NavLink to="/jobs">Задачи</NavLink>
        <NavLink to="/bugs">Баги</NavLink>
        <NavLink to="/stats">Статистика</NavLink>
        <NavLink to="/help" end>Справка</NavLink>
        {!me.is_guest && me.role === "admin" && <NavLink to="/users">Пользователи</NavLink>}
        {!me.is_guest && me.role === "admin" && <NavLink to="/admin/updates">Обновления</NavLink>}
        <span className="who">
          {me.is_guest ? <><span>Гость</span><NavLink to="/login">Войти</NavLink></> : <>
            <NavLink to="/profile">{me.name}</NavLink>
            <button className="link" onClick={signOut}>Выйти</button>
          </>}
        </span>
      </nav>
      <main>
        {error && <p className="message error" role="alert">{error}</p>}
        {!me.is_guest && me.password_change_suggested && (
          <PasswordSuggestion key={me.name} onDismiss={() => setMe((current) =>
            current && !current.is_guest && current.name === me.name ? { ...current, password_change_suggested: false } : current
          )} />
        )}
        <Routes>
          <Route path="/login" element={me.is_guest ? <LoginPage onDone={signedIn} /> : <Navigate to="/" replace />} />
          <Route path="/" element={<DashboardPage canRun={canRun} />} />
          <Route path="/runs" element={<RunsPage />} />
          <Route path="/runs/:id" element={<RunPage />} />
          <Route path="/summaries" element={<ProjectSummariesPage />} />
          <Route path="/summaries/:project/:id" element={<ProjectSummaryPage />} />
          <Route path="/help" element={<HelpCenter />} />
          <Route path="/help/:slug" element={<HelpPageView />} />
          <Route path="/stands" element={<StandsPage key={me.is_guest ? "guest" : me.name} me={me} storageScope={me.is_guest ? "guest" : `user:${me.name}`} />} />
          <Route path="/stands/new" element={canRun ? <StandCreatePage key={me.name} me={me} /> : denied} />
          <Route path="/stands/projects/new" element={!me.is_guest && me.role === "admin" ? <StandProjectCreatePage /> : denied} />
          <Route path="/stands/catalog/:project" element={!me.is_guest && me.role === "admin" ? <StandCatalogPage /> : denied} />
          <Route path="/stands/:project/:id" element={!me.is_guest && ["admin", "tester", "developer", "viewer"].includes(me.role) ? <StandDetailPage key={me.name} me={me} /> : denied} />
          <Route path="/stands/:project/:name/seed" element={canViewTestData ? <SeedPage /> : denied} />
          <Route path="/jobs" element={<JobsPage canRun={canRun} />} />
          <Route path="/batches/:id" element={<BatchPage />} />
          <Route path="/jobs/:id" element={<JobPage canRun={canRun} />} />
          <Route path="/start" element={canRun ? <StartPage role={me.role} /> : denied} />
          <Route path="/bugs" element={<BugsPage />} />
          <Route path="/bugs/:id" element={<BugPage />} />
          <Route path="/stats" element={<StatsPage />} />
          <Route path="/password" element={me.is_guest ? denied : <ChangePassword onDone={refresh} />} />
          <Route path="/profile" element={me.is_guest ? denied : <ProfilePage onPasswordChanged={refresh} />} />
          <Route path="/users" element={!me.is_guest && me.role === "admin" ? <UsersPage /> : denied} />
          <Route path="/admin/updates" element={!me.is_guest && me.role === "admin" ? <UpdatesPage /> : denied} />
          <Route path="/users/:login" element={!me.is_guest && me.role === "admin" ? <UserCardPage me={me.name} /> : denied} />
          <Route path="*" element={<p className="message">Страница не найдена</p>} />
        </Routes>
      </main>
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter><App /></BrowserRouter>
  </StrictMode>,
);
