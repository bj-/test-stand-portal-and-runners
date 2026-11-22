import { useId, useLayoutEffect, useRef, type ReactNode } from "react";

export function CatalogDialog({ title, busy, onClose, children }: { title: string; busy: boolean; onClose: () => void; children: ReactNode }) {
  const box = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useLayoutEffect(() => {
    const dialog = box.current;
    dialog?.showModal();
    dialog?.querySelector<HTMLElement>("form :is(input:not([readonly]):not([type=checkbox]), select):not([disabled])")?.focus();
    return () => dialog?.close();
  }, []);
  return <dialog className="stand-catalog-dialog" ref={box} aria-labelledby={titleId} onCancel={(event) => {
    event.preventDefault();
    if (!busy) onClose();
  }}>
    <header className="stand-catalog-section-head">
      <h3 id={titleId}>{title}</h3>
      <button type="button" className="secondary" disabled={busy} onClick={onClose}>Закрыть</button>
    </header>
    {children}
  </dialog>;
}
