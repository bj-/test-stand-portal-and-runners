import { useEffect, useRef, useState, type ReactNode } from "react";

export type Consequences = { what: string[]; risks: string[] };

/**
 * Подтверждение действия: что будет сделано, чем грозит, осмысленное согласие.
 * Кнопка доступна только после отметки «понимаю» и, для необратимых действий, ввода контрольного слова.
 */
export function ConfirmDialog({ title, info, confirmWord, confirmLabel = "Подтвердить", danger, children, onConfirm, onClose }: {
  title: string;
  info: Consequences;
  confirmWord?: string; // необратимое действие: ввести, например, название стенда
  confirmLabel?: string;
  danger?: boolean;
  children?: ReactNode; // дополнительные поля, например параметры пересборки
  onConfirm: () => Promise<unknown> | void;
  onClose: () => void;
}) {
  const [understood, setUnderstood] = useState(false);
  const [word, setWord] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    box.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const ready = understood && (!confirmWord || word.trim() === confirmWord) && !busy;
  const confirm = () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    Promise.resolve(onConfirm())
      .catch((e: Error) => setError(e.message))
      .finally(() => setBusy(false));
  };

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <div className={`modal ${danger ? "danger" : ""}`} role="dialog" aria-modal="true" aria-labelledby="confirm-title" tabIndex={-1} ref={box}>
        <header><h2 id="confirm-title">{title}</h2></header>
        <div className="modal-body">
          {children}
          <section>
            <h3>Что будет сделано</h3>
            <ul>{info.what.map((line) => <li key={line}>{line}</li>)}</ul>
          </section>
          <section className="risks">
            <h3>Чем это грозит</h3>
            <ul>{info.risks.map((line) => <li key={line}>{line}</li>)}</ul>
          </section>
          <label className="check">
            <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
            Понимаю, что произойдёт, и хочу продолжить
          </label>
          {confirmWord && (
            <label className="confirm-word">
              Для подтверждения введите <code>{confirmWord}</code>
              <input value={word} autoComplete="off" spellCheck={false} onChange={(e) => setWord(e.target.value)} />
            </label>
          )}
          {error && <p className="message error">{error}</p>}
        </div>
        <footer>
          <button type="button" className="link" disabled={busy} onClick={onClose}>Отмена</button>
          <button type="button" className={danger ? "primary danger" : "primary"} disabled={!ready} onClick={confirm}>
            {busy ? "Отправляю…" : confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}

/** Тема оформления: применить и запомнить в браузере (страница входа и первый кадр до загрузки профиля). */
export function applyTheme(theme: string | undefined) {
  const value = theme === "light" || theme === "system" ? theme : "dark";
  document.documentElement.dataset.theme = value;
  try {
    localStorage.setItem("portal-theme", value);
  } catch {
    // хранилище недоступно (приватный режим) - тема всё равно применена
  }
}
