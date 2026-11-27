import { Link, useParams, useSearchParams } from "react-router-dom";
import { getJson, type Project } from "./api";
import { Loaded, Markdown, useLoad } from "./common";

type ProjectSummary = {
  id: string;
  project: string;
  title: string;
  date: string;
  module: string;
  description: string;
  has_presentation: boolean;
};

type ProjectSummaryDetail = ProjectSummary & { markdown: string };

const listUrl = (project: string) => `/summaries?project=${encodeURIComponent(project)}`;
const itemUrl = (project: string, id: string) => `/summaries/${encodeURIComponent(project)}/${encodeURIComponent(id)}`;
const listApi = (project: string) => `/api/projects/${encodeURIComponent(project)}/summaries`;
const itemApi = (project: string, id: string) => `${listApi(project)}/${encodeURIComponent(id)}`;

function SummaryDate({ date }: { date: string }) {
  const [year, month, day] = date.split("-");
  return <time dateTime={date}>{day}.{month}.{year}</time>;
}

export function ProjectSummariesTile({ project }: { project: string }) {
  const state = useLoad(() => getJson<ProjectSummary[]>(listApi(project)), [project]);
  const latest = state.data?.[0];
  return (
    <section className="gpanel summary-tile" aria-label="Сводки тестирования">
      <header>
        <h2>Сводки тестирования</h2>
        <Link className="more" to={listUrl(project)}>Все сводки →</Link>
      </header>
      <div className="gpanel-body">
        {state.error ? <p className="sub">Не удалось загрузить сводки.</p>
          : state.data === null ? <p className="sub">Загрузка сводок…</p>
          : latest ? (
            <div className="summary-latest">
              <div>
                <strong><Link to={itemUrl(latest.project, latest.id)}>{latest.title}</Link></strong>
                {latest.description && <p>{latest.description}</p>}
              </div>
              <span className="sub"><SummaryDate date={latest.date} /> · всего {state.data.length}</span>
            </div>
          ) : <p className="sub">У проекта пока нет сводок.</p>}
      </div>
    </section>
  );
}

function SummaryList({ project }: { project: string }) {
  const state = useLoad(() => getJson<ProjectSummary[]>(listApi(project)), [project]);
  return <Loaded state={state}>{(items) => items.length === 0 ? (
    <p className="message">У проекта пока нет сводок.</p>
  ) : (
    <ul className="summary-list">
      {items.map((summary) => (
        <li className="gpanel" key={summary.id}>
          <div className="gpanel-body">
            <div className="summary-meta sub">
              <SummaryDate date={summary.date} />
              {summary.module && <span className="tag info">{summary.module}</span>}
              {summary.has_presentation && <span className="tag muted">HTML-презентация</span>}
            </div>
            <h2><Link to={itemUrl(summary.project, summary.id)}>{summary.title}</Link></h2>
            {summary.description && <p>{summary.description}</p>}
          </div>
        </li>
      ))}
    </ul>
  )}</Loaded>;
}

export function ProjectSummariesPage() {
  const [params, setParams] = useSearchParams();
  const projects = useLoad(() => getJson<Project[]>("/api/projects"), []);
  const requested = params.get("project");
  return (
    <>
      <header className="page-head"><h1>Сводки тестирования</h1></header>
      <p className="sub">Срезы по модулям и проектам: состав тестов, покрытие, результаты и выводы о готовности.</p>
      <Loaded state={projects}>{(items) => {
        const active = requested === null ? items[0] : items.find((project) => project.code === requested);
        return <>
          {items.length > 0 && (
            <label className="summary-filter">
              Проект
              <select value={active?.code ?? ""} onChange={(event) => setParams({project: event.target.value})}>
                {!active && <option value="" disabled>Выберите проект</option>}
                {items.map((project) => <option key={project.code} value={project.code}>{project.title}</option>)}
              </select>
            </label>
          )}
          {active ? <SummaryList key={active.code} project={active.code} />
            : <p className="message">{items.length > 0 ? "Проект из ссылки не найден." : "Проектов нет."}</p>}
        </>;
      }}</Loaded>
    </>
  );
}

export function ProjectSummaryPage() {
  const { project = "", id = "" } = useParams();
  const url = itemApi(project, id);
  return <SummaryArticle key={url} project={project} url={url} />;
}

function SummaryArticle({ project, url }: { project: string; url: string }) {
  const state = useLoad(() => getJson<ProjectSummaryDetail>(url), [url]);
  return (
    <>
      <p className="summary-back"><Link to={listUrl(project)}>← К сводкам проекта</Link></p>
      <Loaded state={state}>{(summary) => (
        <>
          <header className="page-head summary-head">
            <div>
              <div className="summary-meta sub">
                <SummaryDate date={summary.date} />
                {summary.module && <span className="tag info">{summary.module}</span>}
              </div>
              <h1>{summary.title}</h1>
            </div>
            <div className="summary-downloads">
              <a href={`${url}/report`}>Скачать Markdown</a>
              {summary.has_presentation && <a className="button" href={`${url}/presentation`}>Скачать презентацию HTML</a>}
            </div>
          </header>
          <Markdown resolveUrl={(link) => link === "slides.html" ? `${url}/presentation` : undefined}>
            {summary.markdown}
          </Markdown>
        </>
      )}</Loaded>
    </>
  );
}
