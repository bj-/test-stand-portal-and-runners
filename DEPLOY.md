# Развернуть портал и учебный стенд

Скрипт устанавливает зависимости, поднимает портал с PostgreSQL, затем собирает
учебное API с SQLite, выполняет миграцию, засев, healthcheck и HTTP smoke.
Результаты smoke включают Allure HTML. Это запуск через CLI: учебный стенд пока
не регистрируется в карточках портала и не получает задания от его агента.

## Windows

Откройте PowerShell в корне этого репозитория.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy.ps1 -Workspace C:\stand-lab
```

При первом запуске выберите новый внешний каталог `C:\stand-lab`. Для проверки
плана без изменений добавьте `-Plan`; для диагностики зависимостей — `-CheckOnly`.
Если порт 8080 занят, добавьте `-Port 8081`. Для второй установки используйте
другие `-Workspace`, `-Name` и `-Port` (например `C:\stand-lab2`, `demo2`, `8082`).

Installer запрашивает необходимые права установки. Код 20 означает, что нужно
выполнить напечатанное действие (например, reboot или первый запуск Docker
Desktop), затем повторить ту же команду.
При пустой базе установщик создаёт `admin` со случайным паролем и один раз
показывает его в терминале. При повторном запуске используйте пароль
существующей учётки. Сменить пароль можно в профиле.
Подробнее о зависимостях и поддерживаемых ОС: [installer](installer/README.md).

## Ubuntu / macOS

Из корня этого репозитория:

```bash
bash deploy.sh --workspace "$HOME/stand-lab"
```

Режимы `--dry-run`, `--check`, `--name`, `--port` соответствуют Windows-флагам.
Для собственного совместимого checkout портала передайте `--portal-source`
или `-PortalSource`. Существующая установка использует сохранённый путь.
Для первой сборки нужен интернет: базовые Python/Node images, зависимости портала
и Allure 3.33.0. Docker должен использовать Linux containers и локальный daemon.

## Что проверить после запуска

- Портал открывается по адресу, который напечатал installer (по умолчанию
  `http://127.0.0.1:8080`). Для первого входа используйте пароль из терминала.
- Финальный JSON содержит `status: success` и `output_bindings.baseUrl` — адрес
  учебного API на динамическом loopback-порту. `/health` отвечает `{"status":"ok"}`,
  `/items` содержит запись `demo`, `/version` показывает выбранную ветку.
- В `example/build/execution.json` доступны SHA, стадии, логи, image IDs/digests
  и `attempts[-1].output_dir`. В этом каталоге находятся `allure-report.html`,
  `results.json`, `junit.xml`, `report.md`. Allure должен показывать две проверки.

Каталог установки:

```text
stand-lab/
  portal/                  # настройки и данные портала
  example/
    source/                # созданный скриптом учебный Git, без remote
    catalog.json           # локальный путь учебного репозитория
    request.json
    template.json
    build/
      snapshot.json        # frozen template и полные SHA компонентов
      contexts/            # отдельные committed исходники
      execution.json       # состояния и история попыток
      logs/<attempt>/
      results/<attempt>/   # отдельные результаты каждой попытки
```

SQLite хранится в именованном Docker volume этого стенда. Этот volume — не файл
внутри `stand-lab`; копирование только каталога не является резервной копией БД.
Генерируемый учебный Git содержит `main`, `test` и `feature-demo`. Это локальные
коммиты примера, не коммиты исходного checkout; push нигде не выполняется.

## Повтор, другая ветка и остановка

Если запуск уже дошёл до executor, повторите команду с `-Retry` / `--retry`.
Повтор использует прежний snapshot, снова выполняет стадии и сохраняет прежние
данные. Миграция и засев именно этого примера идемпотентны. После неудачи автоматического
удаления контейнеров/volumes нет. Логи предыдущей попытки остаются доступны.

Отдельный пример с API из feature-ветки и smoke из test (Docker уже установлен):

```powershell
python .\installer\deploy_demo.py --workspace C:\stand-feature --api-ref feature-demo --runner-ref test
```

Для подготовки без Docker добавьте `--prepare-only`. Новая комбинация refs требует
нового workspace. Существующий source и snapshot скрипт не переписывает.
Для собственных clones используйте `stands prepare` с каталогом и шаблоном,
затем `stands run`; [контракт](stands/README.md), [executor](stands/RUNTIME.md).

Остановить учебный стенд, сохранив контейнеры и данные:

```powershell
# Из корня этого репозитория
python -m stands stop --workspace C:\stand-lab\example\build
python local/portal_local.py --workspace C:\stand-lab\portal down
```

Вернуть пример в работу: `python -m stands run --workspace C:\stand-lab\example\build --retry`.
Для долгой первой сборки доступен `--timeout 1800` у `stands run` и `deploy_demo.py`;
по умолчанию общий тайм-аут стадий — 900 секунд. После тайм-аута проверьте состояние
Docker: завершение клиентской команды не гарантирует отмену всех операций daemon.

## Состояние приёмки

Скрипты подготовлены к запуску пользователем. Автотесты на машине разработки
проверяют Git, HTTP/SQLite, подготовку, изоляцию и обработку ошибок с подставным
Docker; проверен синтаксис PowerShell и Bash. Затем пользователь установил Docker
и запустил портал на Windows: HTTP health отвечает ok. Новый первый вход отдельно
проверен с настоящими PostgreSQL/API и Edge. Сквозной прогон учебного стенда через
портал/агент принят на Windows Docker Desktop: сборка, миграция, засев, smoke/Allure,
намеренная ошибка и повтор по тем же SHA с сохранением БД. Установка на чистых
Windows/Ubuntu/macOS ещё не принята.
После запуска для диагностики достаточно кода завершения и каталогов `logs/`,
`results/`, файла `execution.json`; пароль и `.env` пересылать не нужно.

Используются штатные команды [Compose config](https://docs.docker.com/reference/cli/docker/compose/config/)
для проверки модели и [Compose up --wait](https://docs.docker.com/reference/cli/docker/compose/up/)
для ожидания готовности. Входящий шаблон — доверенная конфигурация администратора;
executor не является sandbox для недоверенного кода.

## Агент вместе с порталом

Installer теперь выполняет также `agent-init` и `agent-start`: отдельный ключ
привязывается к локальному хосту, worker запускается в фоне. Повтор установки
сохраняет ключ и настройки; второй worker не запускается. Отдельный ручной запуск
после installer не нужен.

Настройки находятся в `<workspace портала>/agent/config.json` и `agent/hosts.json`.
Каталоги приложений первоначально пусты. Порядок подключения clones, протокол
и API повторов описаны в [документации локального агента](docs/local-stand-agent.md).
Standalone-пример из этого deploy-скрипта по-прежнему запускается напрямую;
для управляемого экземпляра используйте каталог/API портала.

```powershell
python .\local\portal_local.py --workspace C:\stand-lab\portal agent-status
python .\local\portal_local.py --workspace C:\stand-lab\portal agent-stop
python .\local\portal_local.py --workspace C:\stand-lab\portal agent-start
```

После перезагрузки повторите installer либо `agent-start`: служба автозапуска ОС
пока не устанавливается. `down` сначала останавливает агент; если он не подтвердит
остановку за 30 секунд, портал останется работать. Данные стендов не удаляются.
