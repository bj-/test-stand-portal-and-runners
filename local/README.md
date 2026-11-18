# Локальный портал

Launcher поднимает портал с PostgreSQL и управляет фоновым `stands.worker` на хосте.
Installer сам выполняет `agent-init`/`agent-start`; ключи и настройки находятся
в workspace/agent. Docker socket не монтируется в портал. Каталоги приложений
первоначально пусты; настройка — `test-portal/docs/local-stand-agent.md`.
Команды `agent-status`, `agent-stop`, `agent-start` используют тот же `--workspace`.
Системная служба автозапуска ОС пока не устанавливается.

Launcher использует `docker-compose.yaml` из отдельного локального клона
`test-portal` и Python 3.12+ без дополнительных Python-пакетов. Исходники портала
получите заранее принятым в вашей команде способом: launcher ничего не клонирует.
Нужен установленный Docker с Compose v2 и запущенным daemon:
Docker Desktop с Linux containers на Windows/macOS или Docker Engine с Compose
plugin на Ubuntu. Установите их заранее обычным способом для своей ОС.
При первой сборке Docker загружает базовые образы и зависимости продукта из сети.

Запускайте команды из корня `test-stand-portal-and-runners`. Workspace должен находиться
**вне обоих клонов**. Первый `init` требует `--portal-source` с путём к клону портала,
содержащему `docker-compose.yaml` и `Dockerfile`. Путь сохраняется в marker;
последующие команды используют его автоматически. Повторный параметр должен совпадать.
В нём создаётся пустой `projects.yaml`; клиентская конфигурация из клона не копируется.

Windows PowerShell:

```powershell
python local/portal_local.py --workspace "C:/portal work/demo" --portal-source "C:/git/test-portal" init --name demo --port 8080
python local/portal_local.py --workspace "C:/portal work/demo" doctor
python local/portal_local.py --workspace "C:/portal work/demo" up
python local/portal_local.py --workspace "C:/portal work/demo" admin-init
```

Ubuntu и macOS:

```bash
python3 local/portal_local.py --workspace "$HOME/portal work/demo" --portal-source "$HOME/git/test-portal" init --name demo --port 8080
python3 local/portal_local.py --workspace "$HOME/portal work/demo" doctor
python3 local/portal_local.py --workspace "$HOME/portal work/demo" up
python3 local/portal_local.py --workspace "$HOME/portal work/demo" admin-init
```

Откройте <http://127.0.0.1:8080>. `admin-init` создаёт `admin/admin` без терминального
ввода только при пустой таблице пользователей. Повтор сохраняет существующие учётки.
После входа можно сменить пароль или оставить как есть; смена доступна позже в профиле.
Для других пользователей `user-add` вызывает штатный интерактивный CLI:
пароль пользователя вводится дважды, минимум 10 символов. По умолчанию создаётся
администратор; доступны `--role admin|tester|developer|viewer` и `--display-name`.
Для интерактивного ввода используйте обычный терминал. Docker Desktop может
запросить доступ к каталогу workspace.

В последующих командах сохраняйте тот же `--workspace`:

| Команда | Действие |
|---|---|
| `doctor` | Проверить файлы установки, Compose v2 и Docker daemon |
| `up` | Собрать и запустить портал (`up -d --build`) |
| `down` | Остановить установку, сохранив данные |
| `status` | Показать контейнеры, включая остановленные |
| `logs --tail 100 --follow` | Смотреть журнал контейнеров |
| `user-add LOGIN` | Создать учётную запись штатным CLI |
| `admin-init` | Создать admin/admin в пустой базе без TTY; существующие учётки не менять |
| `sync` / `sync --project CODE` | Индексировать настроенные результаты штатным CLI |

`init` работает без Docker и сохраняет существующие настройки при повторе.
Имя и порт задаются при первом `init`: порт от 1024 до 65535, имя из строчных
латинских букв, цифр, `_` и `-`, начинается с буквы/цифры, до 40 символов.
Для второго workspace выберите другой порт. К имени Compose добавляется
устойчивый суффикс от пути workspace; итоговое имя записано в `.portal-local.json`.
Управление каталогом без корректного marker запрещено; существующий непустой
чужой каталог не изменяется. Не переносите marker между установками.

Все данные установки находятся вне Git:

```text
workspace/
  .portal-local.json   # идентификатор, исходники портала, имя Compose и порт
  .env                # случайный пароль PostgreSQL и настройки
  projects.yaml       # первоначально projects: {}
  data/postgres/      # данные БД
  data/artifacts/     # артефакты портала
  results/            # результаты (в контейнере /data/results, только чтение)
```

На Unix `.env` создаётся с правами `0600`; на Windows используются права каталога
вашей учётной записи. Не добавляйте workspace в Git и не публикуйте `.env`.
В выводе обычных Compose-команд launcher скрывает значение пароля БД из `.env`;
остальные секреты в журналах автоматически не распознаются. `user-add` передаёт
терминал напрямую штатному CLI для ввода пароля; его вывод не фильтруется.
Launcher закрепляет bind за `127.0.0.1` и пути за workspace даже при других
значениях в окружении или `.env`. Имя и порт берутся из marker. Из `.env` можно
использовать, например, `BASE_REGISTRY` для доступного реестра базовых образов.
Пароль БД launcher не печатает; его точное значение скрывается в выводе Compose.

Используется только локальный Docker с Linux containers: Unix socket либо локальный
Windows named pipe. Удалённые SSH/TCP contexts отклоняются до обращения к daemon;
выбранный `DOCKER_CONTEXT` имеет приоритет над `DOCKER_HOST`. Системный Docker context
launcher сам не переключает. Для установки отсутствующих зависимостей используйте
[установщик вашей ОС](../installer/README.md).

Это локальная установка портала и host-worker. Docker socket не передаётся порталу;
пустой `sync` не создаёт тесты или результаты. Проекты, каталоги и шаблоны
настраиваются отдельно для этой установки.
`down` не удаляет данные; команд сброса или удаления workspace здесь нет.

Проверка launcher без Docker:

```bash
python -m unittest discover -s local -p "test_*.py" -v
```
