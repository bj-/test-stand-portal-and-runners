import { useEffect, useRef, useState } from "react";
import { ApiError, getJson, sendJson } from "./api";
import { formatDate } from "./common";

type UpdateOperation = {
  id?: string;
  action?: string;
  phase: string;
  message: string;
  current_revision?: string;
  available_revision?: string;
  failure_phase?: string;
  error_code?: string;
};
type UpdateStatus = UpdateOperation & {
  enabled: boolean;
  pause_supported?: boolean;
  paused?: boolean;
  maintenance: boolean;
  last_checked_at?: string;
  last_operation_id?: string;
};
const STORAGE_KEY = "portal:update-operation";
const TERMINAL = new Set(["unchanged", "available", "succeeded", "failed", "recovery_required", "done", "rolled_back", "paused"]);
const BUSY = new Set(["queued", "checking", "preparing", "draining", "backing_up", "activating", "verifying",
  "resuming", "rolling_back", "building", "applying", "restarting"]);
const PHASE_TITLES: Record<string, string> = {
  queued: "ожидание запроса", idle: "ожидание проверки", checking: "проверка обновлений",
  preparing: "подготовка версии", draining: "ожидание задач", backing_up: "резервное копирование",
  activating: "установка", verifying: "проверка портала", resuming: "возобновление задач",
  rolling_back: "восстановление версии", unchanged: "проверка актуальности", available: "проверка новой версии",
  succeeded: "завершение", failed: "завершение с ошибкой", recovery_required: "ручное восстановление",
  building: "сборка", applying: "установка", restarting: "перезапуск", done: "завершение",
  rolled_back: "восстановление версии", error: "завершение с ошибкой", disabled: "подключение службы",
  up_to_date: "проверка актуальности", paused: "обновления приостановлены",
};

function FailureDetail({ value }: { value: UpdateOperation }) {
  if (!value.failure_phase && !value.error_code) return null;
  return <p>
    {value.failure_phase && <>Этап ошибки: {PHASE_TITLES[value.failure_phase] ?? "неизвестен"}. </>}
    {value.error_code === "command_timeout" && "Превышено время ожидания команды."}
    {value.error_code === "update_failed" && "Не удалось выполнить обновление."}
  </p>;
}

function rememberedOperation(): string | null {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return value && /^[0-9a-f-]{36}$/i.test(value) ? value : null;
  } catch { return null; }
}

export function UpdatesPage() {
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [operation, setOperation] = useState<UpdateOperation | null>(null);
  const [operationId, setOperationId] = useState(rememberedOperation);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [policySending, setPolicySending] = useState(false);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const policyPending = useRef(false);
  const policyGeneration = useRef(0);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      const generation = policyGeneration.current;
      const isCurrent = () => active && !policyPending.current && generation === policyGeneration.current;
      try {
        const current = await getJson<UpdateStatus>("/api/admin/updates");
        if (!isCurrent()) return;
        setStatus(current);
        setError(null);
        // Server tracks automatic updates as well as requests from other clients.
        const id = operationId ?? current.last_operation_id;
        if (id && current.enabled) {
          try {
            const detail = await getJson<UpdateOperation>(`/api/admin/updates/${encodeURIComponent(id)}`);
            if (!isCurrent()) return;
            setOperation(detail);
            if (operationId && TERMINAL.has(detail.phase)) {
              try { localStorage.removeItem(STORAGE_KEY); } catch { /* storage may be unavailable */ }
              setOperationId(null);
            }
          } catch (e) {
            if (e instanceof ApiError && e.status === 404) {
              if (isCurrent()) {
                setOperation(null);
                if (operationId) {
                  try { localStorage.removeItem(STORAGE_KEY); } catch { /* optional storage */ }
                  setOperationId(null);
                }
              }
            } else throw e;
          }
        }
      } catch {
        if (isCurrent()) setError("Связь с порталом временно недоступна. Повторное подключение выполняется автоматически.");
      } finally {
        if (active) timer = setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { active = false; clearTimeout(timer); };
  }, [operationId]);

  const changePolicy = async () => {
    if (!status?.enabled || !status.pause_supported || policyPending.current) return;
    policyPending.current = true;
    policyGeneration.current += 1;
    setPolicySending(true);
    setPolicyError(null);
    try {
      const result = await sendJson<{ paused: boolean }>("/api/admin/updates/policy", "PUT", { paused: !status.paused });
      setStatus(current => current ? { ...current, paused: result.paused } : current);
    } catch {
      setPolicyError("Не удалось подтвердить изменение режима обновлений. Проверьте состояние перед повторной отправкой.");
    } finally {
      // Ignore polls started before or during the policy request.
      policyGeneration.current += 1;
      policyPending.current = false;
      setPolicySending(false);
    }
  };

  const request = async (action: "check" | "apply") => {
    setSending(true);
    setError(null);
    try {
      const result = await sendJson<UpdateOperation>(`/api/admin/updates/${action}`, "POST", {});
      setOperation(result);
      setOperationId(result.id ?? null);
      if (result.id) {
        try { localStorage.setItem(STORAGE_KEY, result.id); } catch { /* server remains the source of status */ }
      }
    } catch {
      setError("Не удалось подтвердить приём запроса. Проверьте состояние обновления перед повторной отправкой.");
    } finally { setSending(false); }
  };
  const busy = sending || !!operationId || BUSY.has(status?.phase ?? "") || BUSY.has(operation?.phase ?? "");
  const disabled = !status?.enabled || busy || policySending || !!error;
  return <>
    <h1>Обновления портала</h1>
    <p>Когда обновления включены, служба проверяет Git при запуске и затем каждый час.</p>
    <p>Перед установкой служба ждёт завершения выполняющихся задач. Новые задачи остаются в очереди.
      Во время перезапуска страница восстановит соединение автоматически.</p>
    {error && <p className="message error" role="alert">{error}</p>}
    {policyError && <p className="message error" role="alert">{policyError}</p>}
    {!status && !error && <p className="message">Загрузка…</p>}
    {status && <>
      {!status.enabled && <p className="message">Обновления не настроены. Администратору сервера нужно подключить службу обновлений.</p>}
      <div className="actions">
        <button role="switch" aria-label="Обновления из репозитория" aria-checked={!status.paused}
          disabled={!status.enabled || !status.pause_supported || sending || policySending || !!error}
          onClick={() => void changePolicy()}>
          {policySending ? "Сохранение…" : status.paused ? "Включить обновления из репозитория" : "Отключить обновления из репозитория"}
        </button>
      </div>
      {status.enabled && !status.pause_supported && <p className="message">
        Чтобы включать и отключать обновления здесь, обновите службу обновлений на сервере.
      </p>}
      {status.enabled && status.paused && <p className="message" role="status">
        Проверки по расписанию и установка приостановлены. Ручная проверка доступна.
        Если обслуживание уже началось, служба завершит установку.
      </p>}
      <dl>
        <dt>Установленная версия</dt><dd><code>{status.current_revision ?? "Нет данных"}</code></dd>
        <dt>Доступная версия</dt><dd><code>{status.available_revision ?? "Нет данных"}</code></dd>
        <dt>Последняя проверка</dt><dd>{formatDate(status.last_checked_at ?? null)}</dd>
        <dt>Состояние</dt><dd>{status.message}</dd>
      </dl>
      <FailureDetail value={status} />
      {status.maintenance && <p className="message">Обслуживание: выдача новых задач приостановлена.
        Текущие задачи продолжают выполняться. Возобновлением управляет служба обновлений.</p>}
    </>}
    <div className="actions">
      <button disabled={disabled} onClick={() => void request("check")}>Проверить обновления</button>
      <button disabled={disabled || status?.paused || status?.maintenance || status?.phase === "recovery_required"}
        onClick={() => void request("apply")}>Установить обновление</button>
    </div>
    {operation && <div aria-live="polite" className="message">
      <p>{operation.action === "apply" ? "Установка" : "Проверка"}: {operation.message}</p>
      <FailureDetail value={operation} />
      {operation.id && <p>Номер операции: <code>{operation.id}</code></p>}
    </div>}
  </>;
}
