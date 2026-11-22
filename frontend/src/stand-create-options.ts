import type { StandRepository, StandTemplate } from "./stand-api";

export type ComponentSelection = { repository: string; ref: string };

const versionOrder = new Intl.Collator("en", { numeric: true });
const stableVersion = /^v?\d+(?:\.\d+)*(?:\+[\da-z.-]+)?$/i;

export function groupTemplates(templates: StandTemplate[]) {
  const groups = new Map<string, StandTemplate[]>();
  for (const template of templates) {
    const versions = groups.get(template.id) ?? [];
    versions.push(template);
    groups.set(template.id, versions);
  }
  return [...groups.values()].map((versions) => versions.sort((a, b) =>
    Number(stableVersion.test(b.version)) - Number(stableVersion.test(a.version)) || versionOrder.compare(b.version, a.version),
  ));
}

function byFrequency(values: string[]) {
  const count = new Map<string, number>();
  for (const value of values) count.set(value, (count.get(value) ?? 0) + 1);
  return [...count].sort(([a, aCount], [b, bCount]) => bCount - aCount || a.localeCompare(b, "en"));
}

function compact(values: string[]) {
  return values.length > 2 ? `${values[0]} +${values.length - 1}` : values.join(" + ");
}

function repositoryPath(key: string, repositories: StandRepository[]) {
  const url = repositories.find((repo) => repo.key === key)?.git_url;
  if (url) {
    try { return new URL(url).pathname.replace(/^\/|\/$/g, "").replace(/\.git$/i, "") || key; }
    catch { return key; }
  }
  return key;
}

function sourceLabel(keys: string[], repositories: StandRepository[]) {
  const paths = keys.map((key) => repositoryPath(key, repositories));
  if (paths.length < 2) return paths[0] ?? "";
  const parents = paths.map((path) => path.slice(0, Math.max(0, path.lastIndexOf("/")))).filter(Boolean);
  const [group, count] = byFrequency(parents)[0] ?? ["", 0];
  return count > paths.length / 2 ? group : compact([...new Set(paths)].sort());
}

function refLabel(value: string) {
  return value.trim().replace(/^refs\/heads\//, "").replace(/^refs\/tags\//, "тег ");
}

function shorten(value: string, limit: number) {
  const printable = value.replace(/[\p{C}\p{Z}]+/gu, " ").trim();
  const chars = Array.from(printable);
  return chars.length > limit ? `${chars.slice(0, limit - 1).join("")}…` : printable;
}

export function suggestStandName(project: string, choices: Record<string, ComponentSelection>, repositories: StandRepository[], excludedComponents: readonly string[] = []) {
  const sources = Object.entries(choices);
  const keys = [...new Set(sources.map(([, choice]) => choice.repository).filter(Boolean))];
  const namedSources = sources.filter(([id]) => !excludedComponents.includes(id));
  const refs = byFrequency(namedSources.map(([, choice]) => refLabel(choice.ref)).filter(Boolean)).map(([ref]) => ref);
  return [project, shorten(sourceLabel(keys, repositories), 85), shorten(compact(refs), 75)].filter(Boolean).join(" - ");
}
