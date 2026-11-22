import { Link, useLocation } from "react-router-dom";
import { getJson, type HelpSection } from "./api";
import { Markdown, useLoad } from "./common";

// Документация портала - Markdown-файлы help/NN-имя.md, входят в сборку. Порядок - по номеру в имени файла.
const FILES = import.meta.glob("./help/*.md", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

type Section = { id: string; title: string; markdown: string };

const SECTIONS: Section[] = Object.entries(FILES)
  .sort(([a], [b]) => a.localeCompare(b))
  .map(([path, markdown]) => ({
    id: path.replace(/^.*\/\d+-/, "").replace(/\.md$/, ""),
    title: markdown.match(/^# (.+)$/m)?.[1] ?? path,
    markdown,
  }));

const INSTALLATION = "installation";

export function HelpCenter() {
  const { hash } = useLocation();
  const current = hash.replace(/^#/, "") || SECTIONS[0]?.id;
  const section = SECTIONS.find((s) => s.id === current);
  return (
    <>
      <header className="page-head"><h1>Справка</h1></header>
      <div className="help-center">
        <nav className="help-toc" aria-label="Разделы справки">
          <ol>
            {SECTIONS.map((s) => (
              <li key={s.id}><a href={`#${s.id}`} aria-current={s.id === current ? "page" : undefined}>{s.title}</a></li>
            ))}
          </ol>
          <a className="toc-installation" href={`#${INSTALLATION}`} aria-current={current === INSTALLATION ? "page" : undefined}>
            Эта установка
          </a>
        </nav>
        <article className="help-content">
          {current === INSTALLATION ? <Installation /> : section ? <Markdown>{section.markdown}</Markdown> : <p className="message">Раздел не найден</p>}
        </article>
      </div>
    </>
  );
}

/** Справка конкретной установки: сертификат, инструкции, адреса - из конфигурации (раздел help файла проектов). */
function Installation() {
  const state = useLoad(() => getJson<HelpSection[]>("/api/help"), []);
  return (
    <div className="markdown">
      <h1>Эта установка</h1>
      <p>Ссылки и инструкции, которые задал администратор этой установки портала.</p>
      {state.error && <p className="message error">{state.error}</p>}
      {state.data?.length === 0 && <p>Администратор пока ничего не добавил.</p>}
      {state.data?.map((group) => (
        <section key={group.title}>
          <h2>{group.title}</h2>
          <ul>
            {group.items.map((item) => (
              <li key={item.title + item.url}>
                {item.url.startsWith("/help/")
                  ? <Link to={item.url}>{item.title}</Link>
                  : <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a>}
                {item.note && <> - {item.note}</>}
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}
