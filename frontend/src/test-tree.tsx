import { useMemo, useState, type ReactNode } from "react";

type Item = { id: string; label: string };
type Branch = { key: string; name: string; branches: Map<string, Branch>; leaves: Item[]; ids: string[] };

function makeTree(items: Item[], runner: string): Branch {
  const root: Branch = { key: "[]", name: "", branches: new Map(), leaves: [], ids: [] };
  for (const item of items) {
    const parts = runner === "pytest" ? item.id.split(/\/|::/) : item.id.split("/");
    let branch = root;
    branch.ids.push(item.id);
    const path: string[] = [];
    for (const part of parts.slice(0, -1)) {
      path.push(part);
      if (!branch.branches.has(part)) branch.branches.set(part, { key: JSON.stringify(path), name: part, branches: new Map(), leaves: [], ids: [] });
      branch = branch.branches.get(part)!;
      branch.ids.push(item.id);
    }
    branch.leaves.push(item);
  }
  return root;
}

export function TestTree({ items, visible, runner, selected, setSelected, searching }: {
  items: Item[]; visible: Item[]; runner: string; selected: string[];
  setSelected: (ids: string[]) => void; searching: boolean;
}) {
  const tree = useMemo(() => makeTree(items, runner), [items, runner]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const visibleIds = new Set(visible.map((item) => item.id));
  const selectedIds = new Set(selected);
  const allKeys: string[] = [];
  const collect = (branch: Branch) => { for (const child of branch.branches.values()) { allKeys.push(child.key); collect(child); } };
  collect(tree);
  const renderBranch = (branch: Branch): ReactNode => {
    if (!branch.ids.some((id) => visibleIds.has(id))) return null;
    const open = searching || expanded.has(branch.key);
    const count = branch.ids.filter((id) => selectedIds.has(id)).length;
    return <li key={branch.key}>
      <button type="button" className="tree-toggle" aria-expanded={open} disabled={searching} onClick={() => setExpanded((previous) => {
        const next = new Set(previous); if (next.has(branch.key)) next.delete(branch.key); else next.add(branch.key); return next;
      })}><span aria-hidden="true">{open ? "▾" : "▸"}</span><span className="tree-name">{branch.name || "(пустое имя)"}</span><span className="tree-count">{count}/{branch.ids.length}</span></button>
      {open && renderChildren(branch)}
    </li>;
  };
  const renderChildren = (branch: Branch): ReactNode => <ul className="tree-children">
    {[...branch.branches.values()].map(renderBranch)}
    {branch.leaves.filter((item) => visibleIds.has(item.id)).map((item) => {
      const parts = runner === "pytest" ? item.id.split(/\/|::/) : item.id.split("/");
      return <li key={item.id}><label className="check test-option" title={item.label}><input type="checkbox" checked={selectedIds.has(item.id)} disabled={!selectedIds.has(item.id) && selected.length >= 500} onChange={(e) => setSelected(e.target.checked ? [...selected, item.id] : selected.filter((id) => id !== item.id))} /><span>{parts[parts.length - 1] || item.label}</span></label></li>;
    })}
  </ul>;
  return <>
    <div className="selection-actions"><button type="button" className="secondary" disabled={searching || !allKeys.length} onClick={() => setExpanded(new Set(allKeys))}>Раскрыть всё</button><button type="button" className="secondary" disabled={searching || !allKeys.length} onClick={() => setExpanded(new Set())}>Свернуть всё</button>{searching && <span className="sub">При поиске пути раскрыты автоматически</span>}</div>
    <div className="test-list" aria-label="Существующие тесты">{visible.length ? renderChildren(tree) : <p className="sub">Тесты не найдены.</p>}</div>
  </>;
}
