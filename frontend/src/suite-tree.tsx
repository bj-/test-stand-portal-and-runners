import { useMemo, useState, type ReactNode } from "react";
import type { Suite } from "./api";

type Branch = { key: string; name: string; branches: Map<string, Branch>; leaves: Suite[]; paths: string[] };

function makeTree(suites: Suite[]) {
  const root: Branch = { key: "[]", name: "", branches: new Map(), leaves: [], paths: [] };
  const keys: string[] = [];
  for (const suite of suites) {
    const parts = suite.path.replace(/^suites\//, "").split("/");
    const path: string[] = [];
    let branch = root;
    for (const part of parts.slice(0, -1)) {
      path.push(part);
      if (!branch.branches.has(part)) {
        const key = JSON.stringify(path);
        branch.branches.set(part, { key, name: part, branches: new Map(), leaves: [], paths: [] });
        keys.push(key);
      }
      branch = branch.branches.get(part)!;
      branch.paths.push(suite.path);
    }
    branch.leaves.push(suite);
  }
  return { root, keys };
}

export function SuiteTree({ suites, selected, toggleSuite }: {
  suites: Suite[];
  selected: string[];
  toggleSuite: (path: string, checked: boolean) => void;
}) {
  const { root, keys } = useMemo(() => makeTree(suites), [suites]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const selectedPaths = new Set(selected);
  const renderBranch = (branch: Branch): ReactNode => {
    const open = expanded.has(branch.key);
    const count = branch.paths.filter((path) => selectedPaths.has(path)).length;
    return <li key={branch.key}>
      <button type="button" className="tree-toggle" aria-expanded={open} onClick={() => setExpanded((previous) => {
        const next = new Set(previous);
        if (next.has(branch.key)) next.delete(branch.key); else next.add(branch.key);
        return next;
      })}><span aria-hidden="true">{open ? "▾" : "▸"}</span><span className="tree-name">{branch.name || "(пустое имя)"}</span><span className="tree-count">{count}/{branch.paths.length}</span></button>
      {open && renderChildren(branch)}
    </li>;
  };
  const renderChildren = (branch: Branch): ReactNode => <ul className="tree-children">
    {[...branch.branches.values()].map(renderBranch)}
    {branch.leaves.map((suite) => {
      const name = suite.path.split("/").pop() || suite.path;
      const checked = selectedPaths.has(suite.path);
      return <li key={suite.path}><label className="check test-option" title={suite.description}><input type="checkbox" checked={checked} disabled={!checked && selected.length >= 20} onChange={(e) => toggleSuite(suite.path, e.target.checked)} /><span>{name}{suite.destructive ? " · разрушающий" : ""}</span></label></li>;
    })}
  </ul>;
  return <>
    <div className="selection-actions"><button type="button" className="secondary" disabled={!keys.length} onClick={() => setExpanded(new Set(keys))}>Раскрыть всё</button><button type="button" className="secondary" disabled={!keys.length} onClick={() => setExpanded(new Set())}>Свернуть всё</button></div>
    <div className="test-list" aria-label="Наборы тестов">{suites.length ? renderChildren(root) : <p className="sub">Доступных наборов нет.</p>}</div>
  </>;
}
