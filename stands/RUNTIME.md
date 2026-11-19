# Выполнение подготовленного стенда

`runtime.py` выполняет frozen template из готового workspace без каталога clones,
Git, shell и сторонних Python-пакетов. Нужны Python 3.12+, локальный Docker Engine
с Linux containers и Docker Compose v2 с `config --format json
--no-env-resolution`, `up --wait`. Исходники и шаблон остаются доверенным кодом
администратора; исполнитель не является sandbox для враждебных Dockerfile.

```powershell
python -m stands run --workspace C:/stand-builds/build-001
python -m stands run --workspace C:/stand-builds/build-001 --retry --timeout 1800
python -m stands stop --workspace C:/stand-builds/build-001
```

`--timeout` ограничивает суммарное время дочерних Docker-команд одного вызова,
по умолчанию 900 секунд. Exit 0 означает успех; ошибка CLI возвращает exit 2.
При ошибке или timeout этапы дальше не выполняются; контейнеры, volumes и результаты
сохраняются для разбора. Убитый по timeout клиент Docker не гарантирует отмену уже
отправленной daemon операции. Для остановки контейнеров выполните `stop`.

Порядок: `build → deploy → migrate → seed → healthcheck → smoke`. Пустые migrate
и seed допускаются. Успешный `ps` не доказывает готовность: healthcheck разрешает
`up --wait` либо явную проверку через `run`. Итоговый success устанавливается только
после успешного smoke и получения immutable image IDs. Если шаблон содержит
несколько smoke-команд, любая ошибка останавливает оставшиеся команды.

Повторный `run` всегда требует `--retry`, включая предыдущие failed/stopped/success.
Retry повторяет все этапы, использует прежние project name и volumes. Миграции и
seed должны явно поддерживать повтор в самом шаблоне; исполнитель не объявляет их
идемпотентными. Каждый вызов получает новый UUID попытки и отдельные logs/results.
`stop` выполняет только `docker compose stop` выбранных сервисов: он не удаляет
контейнеры, сети и volumes, а также не меняет результат законченной попытки.

## Допустимые команды

Сейчас `argv` обязано начинаться ровно с `docker`, `compose`, операции. Host-команды
`python`, shell, альтернативные исполняемые файлы и глобальные Compose/Docker flags
запрещены. Опции project, file, env-file, project-directory, context пользовательского
argv не принимаются: исполнитель задаёт их сам каноническими абсолютными путями.

| Этап | Операции и опции |
| --- | --- |
| build | `build [--no-cache] [--pull] [SERVICE ...]` |
| deploy | `up (-d / --detach / --wait) [--no-deps] [--build / --no-build] [--wait-timeout N] [SERVICE ...]` |
| migrate, seed, smoke | `run [--rm] [--no-deps] [--build] [-T] SERVICE [CONTAINER_ARGV ...]` |
| healthcheck | `up --wait ...` либо `run ... SERVICE [CONTAINER_ARGV ...]` |

Без списка сервисов build/up используют только сервисы выбранного профиля.
`run` требует ровно один выбранный SERVICE; после него все токены — буквальный
container argv. `--rm` добавляется автоматически. Подстановки `$()`, `${...}` и
shell-операторы в argv не исполняются. Compose по своим правилам интерполирует
переменные только в Compose-файле. `command.component` проходит проверку контракта,
но команды всегда запускаются с cwd workspace и canonical Compose-файлом;
контексты компонентов доступны через явные переменные.

## Окружение, изоляция и ресурсы

При первом запуске создаётся случайное имя `stand-<UUID>`, которое сохраняется
в `execution.json` и остаётся стабильным при retry/stop. Копия workspace с чужим
execution identity не управляется: сохранённый абсолютный путь должен совпадать.
Внешний `DOCKER_HOST` отклоняется до обращения к daemon; `DOCKER_CONTEXT` имеет
приоритет, его endpoint сначала проверяется локальным `docker context inspect`.
Допустимы только `unix:///...` и локальный `npipe:////./pipe/...`.

Окружение Docker содержит ограниченный набор системных путей, выбор Docker context
и следующие явные значения. Произвольные переменные вызывающего процесса,
`COMPOSE_*` и прежний `BASE_URL` не наследуются; автоматический `.env` отключён,
`--env-file` указывает на пустой контролируемый файл workspace.

| Переменная | Значение |
| --- | --- |
| `OUTPUT_DIR` | Абсолютный `workspace/results/<attempt UUID>` |
| `CONTEXT_<ID>` | Абсолютный `workspace/contexts/<id>` |
| `STAND_PARAM_<NAME>` | Runtime parameter из snapshot; boolean/число в JSON-формате |
| Имена output_bindings | После deploy — URL, полученный через `docker compose port` |
| `BASE_URL` | Alias binding `baseUrl`, если он объявлен |

ID/name для CONTEXT/STAND_PARAM преобразуется в верхний регистр, `.` и `-`
заменяются на `_`; коллизии запрещены. Output bindings не могут подменять управляющие
переменные. Контейнеры получают только environment своего Compose-сервиса;
исполнитель не добавляет `run --env BASE_URL`: loopback URL хоста обычно недоступен
из контейнера. Для runner указывайте внутренний URL, например `http://api:8080`.
`OUTPUT_DIR` предназначен для интерполяции source bind mount; внутри runner следует
задать отдельный путь, например `environment: {OUTPUT_DIR: /results}`.

`config --format json --no-env-resolution` проверяется до build: selected и required
services должны существовать; зависимости должны входить в selected. Проверяются
локальные build contexts/Dockerfile, env_file и bind sources внутри workspace,
отсутствие symlinks/junctions, динамические публикации только `127.0.0.1` (published
`0` либо не задан), project-scoped volumes/networks. Запрещены external/fixed-name
volumes/networks, volume driver options, container_name, host/shared namespaces,
privileged, devices, extra capabilities, volumes_from, Compose configs/secrets,
внешние дополнительные build contexts, SSH/secrets/privileged build inputs и
несколько replicas. Шаблон не должен использовать Compose include/extends для
чтения файлов вне workspace; Compose parsing читает конфигурацию до проверки
нормализованных путей, поэтому происхождение самого шаблона остаётся доверенным.

`runtime.override.json` задаёт CPU и memory из выбранного профиля каждому selected
service, включая runner: `cpus`, `mem_limit` и deploy resource limits. Это лимит
одного контейнера сервиса, не общий бюджет стенда. Несколько сервисов и параллельные
`run`-контейнеры расходуют сумму своих лимитов. Образы и build cache остаются общими
объектами локального Docker; используйте уникальные image tags или автоматически
сформированные project-service имена. Network egress и инструкции Dockerfile не
ограничиваются этим executor.

`secret_refs` сейчас отклоняются целиком. Секреты нельзя помещать в template,
parameters, Compose или argv. Окружение целиком, rendered Compose config и секреты
в execution.json не сохраняются. Stage logs содержат вывод Docker и приложений:
ответственность за отсутствие секретов в их выводе остаётся у шаблона.

## Диагностика и durable state

```text
workspace/
  snapshot.json                 # прежний переносимый frozen snapshot
  contexts/                     # прежние exports выбранных SHA
  execution.json                # локальный durable runtime state
  runtime.override.json         # лимиты выбранного профиля
  .runtime.env                  # пустой контролируемый env-file
  logs/<attempt UUID>/          # preflight, build, deploy, ... smoke, images
  results/<attempt UUID>/        # OUTPUT_DIR для артефактов runner
```

State записывается атомарно до запуска, при переходах этапов и при завершении.
Он хранит project identity, hash snapshot/template, исходные component SHA,
профиль, UUID и статусы попыток, относительные пути logs/output_dir, resolved
bindings и после успеха image IDs/RepoDigests/RepoTags. Локально построенный образ
может не иметь registry digest; тогда `repo_digests` пуст, `id` остаётся обязательным.
Ошибки stop находятся в отдельном массиве stops. Данные каждой попытки сохраняются.

Эксклюзивный `.execution.lock` не позволяет одновременно выполнять run/stop одного
workspace. Автоматического снятия stale lock нет. После аварийного завершения
процесса сначала проверьте PID/контейнеры и только затем вручную удалите lock.
Незавершённая попытка остаётся running как свидетельство прерывания; retry создаёт
новую попытку. Hash проверяет frozen template и целостность runtime identity, но
не является подписью. SHA фиксируют происхождение подготовленных contexts;
последующие ручные изменения файлов contexts не проверяются против Git objects.

Подготовленный нейтральный пример полного развёртывания находится в
[examples/template-demo](../examples/template-demo/README.md). Реальный запуск
принят на Windows Docker Desktop через PostgreSQL/API/worker: успех, ошибка smoke
и повтор по тем же SHA с сохранением данных. Ubuntu/macOS ещё требуют приёмки.

Worker вызывает Python API `run(..., ports=..., progress=..., cancel=...)`.
Порты — точная map output binding → host port из задания портала. После проверки
динамического исходного Compose формируется `runtime.effective.json` с заменёнными
портами и лимитами. Hash эффективной конфигурации и резервации входят в identity.
Standalone CLI без этой map по-прежнему использует динамические порты.
`publish_ip` из конфигурации установки позволяет опубликовать зарезервированные
порты на выделенном частном IPv4. Compose-источник по-прежнему проходит проверку
динамических loopback-портов. При повторе и остановке сохраняется исходный адрес.
После deploy исполнитель записывает разрешённые ссылки в
`${OUTPUT_DIR}/output-bindings.json` для следующих стадий и сервисов.
`progress` сообщает стадии, `cancel` позволяет прервать собственный Docker CLI;
на Windows завершаются также дочерние процессы Compose и Buildx. Данные и
контейнеры при этом сохраняются.
