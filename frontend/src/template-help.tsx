import { useId, useRef, useState } from "react";
import example from "./stand-template-example.json?raw";
import type { StandDeploymentConfiguration, StandRepository } from "./stand-api";

function templatePrompt(repositories: StandRepository[], deployments: StandDeploymentConfiguration[]) {
  const available = repositories.filter((repository) => repository.enabled)
    .map(({ key, components }) => ({ key, components }));
  const defaults = deployments.filter(({ refs }) => refs.length > 0).map(({ stand, refs }) => ({
    stand,
    refs: Object.fromEntries(refs.filter((ref) => ref.default).map(({ name, default: revision }) => [name, revision])),
  }));
  return `Подготовь JSON-файл шаблона стенда portable v1 для test-portal.

Сначала изучи Compose, Dockerfile, README и команды сборки, миграций, засева, healthcheck и smoke в исходниках. Подтверди по ним пути, ветки, сервисы и команды. Если данных недостаточно или действующая конфигурация требует возможностей, которых нет у portable v1, объясни, чего не хватает. Уточни компоненты, зависимости, refs, ресурсы и способ получения Allure-отчёта.

Ключи и компоненты разрешённых репозиториев
${available.length ? JSON.stringify(available, null, 2) : "Каталог пока пуст или недоступен. Запроси ключи репозиториев и их компоненты. Поле repository должно совпадать с ключом в каталоге портала и хоста."}
${defaults.length ? `\nРевизии по умолчанию при пересборке действующих стендов. Проверь, применимы ли они к новому экземпляру.
${JSON.stringify(defaults, null, 2)}\n` : ""}

Контракт portable v1

- Верни один JSON-объект без Markdown и комментариев с полями schema_version, id, version, components, compose, profiles, lifecycle, parameters, secret_refs, smoke, output_bindings. Укажи schema_version = 1. Для новой редакции увеличь version. Портал хранит зарегистрированные версии без изменений.
- Каждый элемент components содержит id, repository (ключ каталога), default_ref (существующая ветка, тег или полный SHA) и dependencies (ID компонентов, без циклов). ID начинаются с латинской буквы, допускают буквы ASCII, цифры, точку, дефис и подчёркивание и занимают до 128 символов.
- В compose укажи component (ID компонента с Compose-файлом), path (относительный путь внутри него, с /, без ..) и непустой required_services. Каждый профиль содержит services и resources {cpus: положительное число, memory_mb: положительное целое}. Профиль laptop обязателен. Включи required_services в каждый профиль. Лимиты применяются к каждому контейнеру.
- В lifecycle укажи массивы для build, deploy, migrate, seed, healthcheck и smoke. Массивы migrate и seed можно оставить пустыми. Каждая команда имеет вид {component: ID, argv: массив строк}. Используй docker compose build для build, docker compose up -d для deploy, docker compose run --rm --no-deps SERVICE [аргументы контейнера] для migrate, seed и smoke, docker compose up --wait или docker compose run для healthcheck. Передавай аргументы без shell, подстановок и глобальных флагов -f/-p/--env-file. Миграции и засев должны выдерживать повторный запуск.
- Для стенда, который можно использовать при падении smoke, добавь необязательный объект liveness. Ключи — долговременные сервисы Compose, значения — команды {component, argv} вида docker compose run --rm --no-deps RUNNER [аргументы контейнера]. Задай ограниченную по времени проверку отклика каждого сервиса по его протоколу. Нулевой код подтверждает отклик. Отклик всех выбранных проверок после падения smoke даёт статус «Работает с ограничениями».
- В parameters укажи спецификации {type, default?, enum?} с типами string, integer, number или boolean. Для действующего исполнителя secret_refs должен быть пустым массивом. Держи пароли, токены, приватные адреса и персональные данные вне шаблона, параметров и команд.
- В smoke укажи runner (ID компонента), env (имя окружения) и suite (относительный путь существующего набора). Проверь готовность приложения и сохрани Allure-отчёт через OUTPUT_DIR.
- В output_bindings сопоставь каждое имя с {service, port, scheme, path, title}. В title укажи понятное название продукта или компонента, на который ведёт ссылка. Для прокси используй название приложения за прокси. Возьми service из required_services. Для port допустимы целые от 1 до 65535, для scheme допустимы http и https. Начинай path с /. Для основного адреса используй baseUrl, если он нужен. Технические имена вроде baseUrl используй как ключи JSON.

В Compose используй CONTEXT_<ID> для контекстов, STAND_PARAM_<NAME> для параметров и OUTPUT_DIR для результатов. В именах переменных используй верхний регистр и заменяй точки и дефисы на _. Из контейнера runner обращайся к приложению по имени его сервиса в сети Compose. Публикуй порты динамически на 127.0.0.1. Используй изолированные сети и тома проекта и пути внутри workspace. Исключи container_name, external/fixed-name volumes/networks, privileged и host namespaces. Проверь, что все сервисы существуют и их зависимости входят в профиль.

Учебный пример ниже использует repository=demo, ветку test, учебные сервисы и tasks.py. Подставь данные из целевого проекта. Проверь итог валидатором portable v1 и отдельным пробным развёртыванием.

${example.trim()}`;
}

export function TemplateHelp({ repositories, deployments }: {
  repositories: StandRepository[]; deployments: StandDeploymentConfiguration[];
}) {
  const promptId = useId();
  const promptBox = useRef<HTMLTextAreaElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [copyStatus, setCopyStatus] = useState("");
  const prompt = templatePrompt(repositories, deployments);
  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(prompt);
      setCopyStatus("Промпт скопирован.");
    } catch {
      setExpanded(true);
      setCopyStatus("Буфер обмена недоступен. Выделите текст промпта ниже и скопируйте его вручную.");
    }
  }
  return <div className="stand-catalog-template-help">
    <p className="sub">Подготовьте JSON по учебному примеру или передайте промпт своей LLM вместе с исходниками проекта. Пример нужно адаптировать к вашим репозиториям и Compose.</p>
    <div className="links">
      <a href={`data:application/json;charset=utf-8,${encodeURIComponent(example)}`} download="stand-template-example.json">Скачать пример JSON</a>
      <button type="button" className="secondary" onClick={() => void copyPrompt()}>Скопировать промпт для LLM</button>
      <button type="button" className="link" aria-expanded={expanded} aria-controls={promptId} onClick={() => setExpanded((value) => !value)}>{expanded ? "Скрыть промпт" : "Показать промпт"}</button>
    </div>
    {copyStatus && <p role="status" className="sub">{copyStatus}</p>}
    <div id={promptId} hidden={!expanded}>
      <label className="stand-catalog-prompt-label">Промпт для подготовки шаблона<textarea ref={promptBox} readOnly value={prompt} rows={14} onFocus={() => promptBox.current?.select()} /></label>
    </div>
  </div>;
}
