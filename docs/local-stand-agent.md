# Локальный исполнитель шаблонов

Портал и агент можно запускать на одной машине. Агент забирает `stand-build`,
исполняет переносимый шаблон через Docker Compose и возвращает результаты.
[Выполнение подготовленного стенда](../stands/RUNTIME.md) и [API создания](stand-management-api.md).

## Установка

Актуальный `test-stand-portal-and-runners/deploy.ps1` или `deploy.sh` вызывает installer,
который запускает портал и агент вместе. Пользовательские пароли не запрашиваются
повторно и не меняются. В workspace портала появляются:

```text
agent/config.json       # private host-worker settings
agent/token             # agent credential; не добавлять в Git
agent/hosts.json         # каталог, который читает портал
agent/mirrors/           # локальные mirrors, доступные порталу read-only
agent/agent.log          # журнал процесса
agent/work/             # contexts, снимки, состояния и результаты
agent.compose.json      # read-only mounts и переменная PORTAL_STAND_HOSTS
```

Launcher-команды с тем же `--workspace`: `agent-status`, `agent-start`, `agent-stop`,
`agent-init`. Последняя повторяет привязку ключа к хосту в БД и не вращает ключ.
`down` сначала просит агент остановиться; при таймауте останавливать портал не будет.
После перезагрузки повторите installer или `agent-start`: системная служба ОС
на этом шаге не устанавливается.

## Каталоги приложения

Автоматическая установка создаёт пустой каталог. Чтобы подключить приложение,
администратор добавляет локальный clone и разрешённые компоненты в `agent/config.json`:

```json
{
  "schema_version": 1,
  "portal_url": "http://127.0.0.1:8080",
  "token_file": "C:/stand-lab/portal/agent/token",
  "host_id": "local",
  "work_root": "C:/stand-lab/portal/agent/work",
  "catalogs": {
    "DEMO": {"schema_version": 1, "repositories": [
      {"id": "demo", "path": "C:/stand-lab/portal/agent/mirrors/demo", "components": ["api", "smoke"]}
    ]}
  },
  "fetch": false,
  "poll_seconds": 2,
  "timeout": 900
}
```

Пути — пример для Windows; на Ubuntu/macOS задаются абсолютные пути этой машины.
Затем в `agent/hosts.json` для того же хоста/проекта добавляется путь зеркала,
видимый **контейнеру портала**: `"repositories": {"DEMO": {"demo": "/stand-mirrors/demo"}}`.
Диапазон портов сохраняется в записи хоста. Project/repository/template регистрируются
через [API каталога](stand-management-api.md). После изменения конфигурации worker
перезапускается через `agent-stop` и `agent-start`.

`fetch: false` использует существующие локальные refs. `fetch: true` требует origin,
обновляет его ветки/теги без checkout и прекращает подготовку при ошибке или таймауте.
Доступ к Git настраивается на хосте; credentials не помещаются в шаблон/задание.
Если клоны обновляет отдельная служба установки, задайте `ref_source: "origin"`
в конфигурации агента и записи хоста. Тогда обе стороны читают
`refs/remotes/origin/*` и `refs/remotes/origin-tags/*`.
`publish_ip` по умолчанию равен `127.0.0.1`. Для публикации на выделенном частном
IPv4 укажите этот адрес в обеих конфигурациях. Исполнитель использует зарезервированные
порты, а портал проверяет адрес и порты в итоговых ссылках.

## Протокол и результаты

Агент вызывает claim с `host_id` и capability `stand-template-v1`. Ключ должен быть
привязан к этому хосту; произвольное объявление capability не даёт заданий.
Для шаблонов с каталогом `liveness` нужна capability `stand-template-v1-liveness`.
До Docker выполняется `stand/snapshot`, затем `stand/progress`, загрузка
`stand/artifacts/{name}` и `stand/finish` внутри `/api/agent/jobs/{job_id}/`.

Пользовательские маршруты:

| Путь после `/api` | Результат |
|---|---|
| `GET /projects/{code}/stand-instances/{id}/builds` | История попыток |
| `GET /stand-builds/{build_id}` | SHA компонентов, этапы, образы и ссылки на артефакты |
| `GET /stand-builds/{build_id}/artifacts/allure-report.html` | Allure в браузере; нужен вход пользователя |
| `POST /projects/{code}/stand-instances/{id}/retry` | Новый запуск прежнего snapshot; тело `{"build_id":"build-1"}`, обязательный `Idempotency-Key` |

Повтор сохраняет Compose project, volumes и прошлые отчёты. Смена ветки в Git
сохраняет уже зафиксированный snapshot. Если smoke завершился с ненулевым
кодом, агент выполняет `liveness` для сервисов активного профиля. Успех всех
выбранных проб публикует адреса и даёт стенду статус `limited`. Задача и сборка
завершаются как `done`, история хранит `smoke:failed` и `liveness:success`.
Для полного успеха нужен Allure. После ошибки smoke агент загружает созданные
артефакты. Отмена через обычный `/jobs/{id}/cancel` сохраняет данные стенда.
Динамические окружения проверяет smoke шаблона.
