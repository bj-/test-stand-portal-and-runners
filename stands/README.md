# Шаблон стенда v1 и подготовка состава

Пакет закрывает ограниченный шаг D1: проверяет переносимый контракт и готовит
изолированные build contexts из Git-коммитов. Python 3.12 и Git; зависимостей Python
нет. Команды подготовки ниже не запускают Docker. Для выполнения lifecycle
добавлен [executor run/stop](RUNTIME.md) и [готовый сценарий развёртывания](../DEPLOY.md).
Очередь подключена через `stands.worker`; мастер создания стенда реализован
в отдельном проекте `test-portal` (b3bd943).
Полный этап D с продуктовым адаптером и проверкой трёх ОС не завершён.

Из корня checkout:

```powershell
python -m stands validate --template stands/examples/http-smoke.template.json
python -m stands validate --template template.json --catalog C:/stand-install/catalog.json --request request.json
python -m stands prepare --template template.json --catalog C:/stand-install/catalog.json --request request.json --workspace C:/stand-builds/build-001
python -m stands replay --snapshot C:/stand-builds/build-001/snapshot.json --catalog C:/stand-install/catalog.json --workspace C:/stand-builds/build-002
python -m unittest discover -s stands -p "test_*.py" -v
```

`validate` без каталога проверяет только контракт; с каталогом и request — также
совместимость локальных clones и выбранных значений. Доступность refs проверяет
`prepare`. Успех: exit 0 и JSON в stdout; ошибка: exit 2 и сообщение в stderr.
Родитель workspace должен существовать. Workspace должен быть новым или пустым,
вне этого репозитория и всех clones каталога; вложенность в обе стороны запрещена.

## Три отдельных входа

Шаблон — доверенная администраторская конфигурация. См.
[нейтральный пример](examples/http-smoke.template.json): он описывает предполагаемые
clones API и runner, содержащие `compose.yaml` и `runner.py`, и сам их не поставляет.
Этот ранний пример иллюстрирует схему, его host-команда Python не поддерживается
Compose executor. Запускаемый шаблон: [template-demo](../examples/template-demo/template.json).

Все поля обязательны, кроме явно перечисленных ниже. Неизвестные поля на любом
уровне, повторяющиеся JSON-ключи, неверные типы и нечисловые NaN/Infinity запрещены.
`true` не считается integer/number. Идентификаторы: буква, затем буквы ASCII,
цифры, `_`, `-`, `.`; до 128 символов. Version — метка до 128 символов, может
начинаться с цифры и включать `+`. Пути переносимые, через `/`, без абсолютных
путей, `..`, `.git`, Windows reserved names, control characters и обратного слеша.

| Поле шаблона | Смысл |
|---|---|
| `schema_version` | Число `1` |
| `id`, `version` | ID и версия доверенного шаблона |
| `components` | Массив `{id, repository, default_ref, dependencies}`; repository — ID из каталога установки, dependencies — component IDs; циклы/пропуски запрещены |
| `compose` | `{component, path, required_services}`; путь относительно context компонента, файл должен существовать в выбранном коммите |
| `profiles` | Map профилей; `laptop` обязателен. Каждый `{services, resources: {cpus, memory_mb}}` содержит все required_services, положительные CPU и целое число MiB |
| `lifecycle` | Ровно `build`, `deploy`, `migrate`, `seed`, `healthcheck`, `smoke`, в этом порядке при будущем выполнении. Массивы `{component, argv}`; только migrate/seed могут быть пустыми |
| `parameters` | Map имён в `{type, default?, enum?}`; тип string/integer/number/boolean, без default значение обязательно в request |
| `secret_refs` | Уникальные имена ссылок на секреты, без значений; разрешаются только будущим executor из локальной установки |
| `smoke` | `{runner, env, suite}`; runner — component ID, env — безопасное имя файла окружения (может начинаться цифрой), suite — переносимый относительный путь к набору проверок |
| `output_bindings` | Сопоставляет ключ адреса с `{service, port, scheme, path, title?}`. Сервис входит в каждый профиль, port — порт контейнера от 1 до 65535, scheme — http/https, title — подпись ссылки для мастера и карточки. |

Профили и команды являются декларацией. Здесь не проверяются синтаксис Compose,
реальное наличие объявленных сервисов в Compose, применимость ограничений CPU/RAM,
работоспособность команд и smoke. Порядок зависимостей не переставляет явно
заданный lifecycle. `argv` — доверенные аргументы без shell и без подстановок;
получатель будущего шага отвечает за cwd, compose project name, порты и executor.

Каталог установки хранится снаружи checkout и переносимого снимка:

```json
{
  "schema_version": 1,
  "repositories": [
    {"id": "service-src", "path": "C:/sources/service", "components": ["api"]},
    {"id": "runner-src", "path": "C:/sources/runner", "components": ["smoke"]}
  ]
}
```

`path` — абсолютный существующий корень локального non-bare clone/worktree.
Никаких clone/fetch, URL или credentials; доступность объектов обеспечивает
администратор. `components` — разрешённые component IDs для этого repository ID.
Каталог заново задаётся для каждой машины; одинаковый ID может указывать на другой
локальный clone с нужными объектами. Шаблон и снимок не знают локальных путей.

Пользовательский request:

```json
{
  "schema_version": 1,
  "profile": "laptop",
  "components": {
    "api": {"ref": "test"},
    "smoke": {"repository": "runner-src", "ref": "feature/new-smoke"}
  },
  "parameters": {"timeout_seconds": 45}
}
```

`components` и `parameters` могут быть пустыми объектами: берутся default refs,
repository IDs и значения parameters. Профиль выбирается явно. Repository override
разрешён только для совместимого component ID в каталоге. Request не может задавать
URL репозитория, команды, Compose-пути, credentials, secret values, lifecycle или
новые параметры. Значения параметров не подставляются и не исполняются.

Refs — существующие локальные ветки, tags либо полный SHA коммита (40 или 64 hex).
Можно явно писать `refs/heads/test` или `refs/tags/v1`; неоднозначное короткое имя
ветки/tag отвергается. Remote-tracking refs, HEAD, опции, сокращённые SHA как SHA
и revision expressions (`~`, `^`, `:`, `@{...}`) не принимаются. Строка из коротких
hex может быть только фактическим именем branch/tag. Нет fallback на main и fetch.
Annotated tags разрешаются в commit; полный SHA самого tag object не принимается.

## Снимок и повтор

`prepare` разрешает выбранный ref в SHA один раз для каждого компонента и затем
читает только эти Git objects. Ни checkout, ни index, ни HEAD исходных clones
не меняются; dirty/staged/untracked файлы не попадают в contexts. Git hooks,
фильтры и archive export-ignore/export-subst не выполняются. Окружение `GIT_*`
не может подменить источник объектов.

Результат:

```text
build-001/
  .stand-build-v1
  snapshot.json
  contexts/
    api/       # committed contents без .git
    smoke/
```

Snapshot v1 содержит `schema_version`, полный frozen `template`, его
`template_sha256` (SHA-256 от UTF-8 JSON с sorted keys, compact separators,
ensure_ascii=false), `components: [{id, repository, requested_ref, sha}]`, `profile`,
полностью разрешённые `parameters`, `secret_refs`. Local paths и URL каталога,
credentials и значения секретов туда не копируются. Шаблон и объявленные runtime
параметры тоже должны быть свободны от секретов: их содержимое переносимо.
Hash проверяет согласованность шаблона, он не является цифровой подписью снимка.

`replay` проверяет снимок и берёт **только сохранённые SHA и frozen template**.
Изменение исходного template или движение веток никак не влияет на повтор.
Недоступный commit — ошибка, без восстановления из текущего состояния ветки.
В snapshot нет timestamps или локального destination; повтор может дать тот же
snapshot JSON на другой машине. Экспорт воспроизводит committed bytes и executable
bit на POSIX; executable bit на Windows не применяется. LFS pointers остаются
committed pointers. Tracked symlinks, gitlinks/submodules, специальные файлы,
не-UTF-8 и непереносимые/коллидирующие по регистру пути отвергаются целиком.
Symlinks и junctions на пути workspace запрещены; ссылки из dirty worktree вообще
не читаются. Это экспорт исходников, не sandbox для последующего запуска кода.

До публикации результат создаётся в собственном sibling staging. При ошибке
удаляется только этот staging; snapshot в target не появляется. Публикация — rename
в пределах одной файловой системы, с маркером. Существующий пустой target удаляется
только через `rmdir` непосредственно перед rename; содержимое никогда не удаляется.
Непустой target, включая прежнюю сборку с маркером, не перезаписывается. После
ошибки безопасно повторить ту же команду; после успеха выберите новый workspace.
Одновременная враждебная подмена каталогов другим процессом не входит в модель
доверия: root workspace должен контролироваться администратором установки.

## Интеграция с порталом

`output_bindings.baseUrl` описывает адрес API. Executor после назначения
host port сохраняет URL в `execution.json`. Будущий адаптер должен записывать
`baseUrl` в `<PROJECT>/.local/<env>.yaml` для
test-engine; базовое `environments/<env>.environment.yaml` уже должно существовать.
Overlay каталог передаётся через `AGENT_TESTS_OVERLAY_DIR`. Сам пакет YAML и overlay
не создаёт. `smoke.env` и `smoke.suite` определяют окружение и suite будущего runner.
Для существующих portal metadata нужен явный mapping компонентного `id` в `name`
и `sha` в `commit`, затем `--product-commit name=SHA`; здесь это не подключено.
