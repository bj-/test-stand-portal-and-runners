# Учебный HTTP-стенд

Это учебный пример, не продуктовый клиентский стенд. Интеграция с portal agent,
Allure и выбором ветки — отдельный следующий этап.

Нужны Docker Engine / Docker Desktop и Docker Compose v2. Пример рассчитан на
Linux-контейнеры в Windows, Ubuntu и macOS. Выполняйте команды из
`examples/http-smoke`. Фактическая контейнерная проверка пока не выполнена:
в среде разработки Docker отсутствует.

Приложение на Python stdlib отвечает JSON на `GET /health` и `GET /version`.
Данные не сохраняются; именованных volumes нет. Порт опубликован только на
`127.0.0.1`, по умолчанию `18080`. Runner обращается к `http://app:8080` внутри
сети конкретного Compose-проекта, ожидает успешный `/health` до 30 секунд,
проверяет оба endpoint и возвращает код `1` при ошибке. Timeout одного запроса —
2 секунды; он может немного продлить ожидание готовности.

Перед запуском задайте **абсолютный внешний каталог** результатов, вне репозитория.
Для каждого стенда/прогона используйте свой каталог: повторный запуск перезаписывает
`junit.xml` и `results.json`. Runner запускается с пользователем образа по умолчанию;
на Linux созданные им файлы могут принадлежать root.

PowerShell:

```powershell
$env:OUTPUT_DIR = Join-Path $env:TEMP ('http-smoke-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $env:OUTPUT_DIR | Out-Null
$env:STAND_PORT = '18080'
```

Ubuntu / macOS:

```sh
export OUTPUT_DIR="$(mktemp -d)"
export STAND_PORT=18080
```

Запуск, проверка и остановка **только проекта `demo1`**:

```sh
docker compose --project-name demo1 up --build -d app
docker compose --project-name demo1 --profile test run --rm runner
docker compose --project-name demo1 down
```

Не выполняйте `down` до завершения runner. Результаты остаются в `OUTPUT_DIR` после
остановки. Для второго независимого экземпляра укажите другое имя проекта, другой
`STAND_PORT` и другой `OUTPUT_DIR` во всех его командах. Не добавляйте `container_name`:
Compose сам разделяет сервисы и сети по имени проекта.

Runner использует тот же Dockerfile, что и app. Команда `up --build` собирает app;
`run` автоматически собирает runner при первом запуске. После изменения исходников
runner пересоберите явно:

```sh
docker compose --project-name demo1 --profile test build runner
```

Проверки без Docker, Python 3.12 или новее, без дополнительных пакетов:

```sh
python -B -m unittest -v test_example.py
```

Они поднимают приложение на временном локальном порту и проверяют endpoint,
успешный runner, неверную версию и недоступный API. Runner вызывается отдельным
процессом; проверяются код выхода и оба формата отчёта. Временные результаты
создаются вне репозитория и автоматически удаляются.

Для ручного запуска без Docker: `python -B app.py` (порт `8080`), затем в другом
терминале `python -B runner.py --base-url http://127.0.0.1:8080 --output-dir <каталог>`.
Замените `<каталог>` абсолютным внешним путём. Остановка приложения — `Ctrl+C`.
