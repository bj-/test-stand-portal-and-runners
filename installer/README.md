# Установка без LLM

Bootstrap устанавливает недостающие зависимости и запускает локальный портал с
PostgreSQL. Он работает как обычная программа: LLM, Codex и Python на чистой
машине не требуются для начала установки. Нужны интернет, права на установку
программ и поддерживаемая Docker платформа с виртуализацией. Сборка образа
загружает зависимости продукта из сети; это не автономная установка.

Получите ZIP этого репозитория и ZIP или клон продукта `test-portal` доступным
вам способом. Распакуйте в **разные** каталоги. Git для получения ZIP не нужен;
bootstrap установит Git для дальнейшей работы. Приватные исходники installer
сам не скачивает. `--portal-source`/`-PortalSource` указывает на корень продукта,
где находятся `Dockerfile` и `docker-compose.yaml`.

Откройте терминал в распакованном `test-stand-portal-and-runners`. Выберите одну
из трёх команд для своей ОС, подставив фактический путь к исходникам продукта.
Workspace должен быть новым каталогом вне обоих наборов исходников.

Windows, обычный PowerShell (Windows PowerShell 5.1 поддерживается):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -Workspace "C:\Portal work\demo" -PortalSource "C:\src\test-portal" -CreateAdmin
```

Ubuntu 24.04 или новее, обычный пользователь с `sudo`:

```bash
bash installer/install.sh --workspace "$HOME/portal work/demo" --portal-source "$HOME/src/test-portal" --create-admin
```

macOS, обычный пользователь с правами установки программ:

```bash
bash installer/install.sh --workspace "$HOME/portal work/demo" --portal-source "$HOME/src/test-portal" --create-admin
```

Флаг создания администратора опционален. С ним пароль `admin` вводится дважды в
терминале, минимум 10 символов; пароль не передаётся в аргументах и не сохраняется
installer. Без флага bootstrap выводит точную команду `user-add admin`, которую
можно выполнить позже. После запуска и HTTP health выводится адрес портала:
по умолчанию <http://127.0.0.1:8080>. Для другой установки задайте
`--name demo2 --port 8081` либо `-Name demo2 -Port 8081` при первом запуске.

Что устанавливается при отсутствии:

| Платформа | Зависимости |
|---|---|
| Windows | Python 3.12 и Git через `winget`; WSL при необходимости; Docker Desktop через `winget` |
| Ubuntu 24.04+ | Python 3, Git, curl и CA; Docker Engine, CLI, containerd, Buildx и Compose plugin из официального Docker apt repo |
| macOS | Homebrew через официальный installer, Python 3.12 и Git через Homebrew; cask `docker-desktop` |

Существующие Python ≥3.12, Git и работающий локальный Docker с Linux containers
используются повторно. Установка зависимостей не обновляет их принудительно.
Конфликтующие Docker-пакеты Ubuntu автоматически не удаляются; сообщение укажет,
что нужно разобрать по официальной инструкции. Если уже установлен Docker без
Compose, Ubuntu bootstrap просит установить совместимый plugin, сохраняя текущую
установку. `sudo` вызывается для установки и настройки службы; весь скрипт
Ubuntu запускать от root не нужно.

Если нужны перезагрузка, первый запуск Docker Desktop или обновление группы
`docker`, программа завершится с **кодом 20** и напечатает точную команду для
продолжения. Выполните указанное действие и повторите ту же команду. Автоматической
перезагрузки нет. На Windows для включения WSL может потребоваться отдельный
PowerShell от администратора с `wsl --install --no-distribution`; затем вернитесь
к обычному терминалу. Если нет `winget`, установите/обновите App Installer из
Microsoft Store и повторите bootstrap.

На Windows и macOS откройте Docker Desktop и самостоятельно завершите первый
запуск и запросы соглашения. Installer не принимает соглашение Docker за вас.
Перед установкой ознакомьтесь с [условиями Docker Desktop](https://docs.docker.com/subscription/desktop-license/)
и требованиями вашей организации. На Ubuntu добавление пользователя в группу
`docker` требует полного выхода из сеанса и повторного входа; эта группа даёт
расширенные полномочия на хосте. Права socket не ослабляются.

Повторный bootstrap сохраняет пароль БД, имя, порт и файлы workspace. После
успешного создания `admin` записывается `.installer-admin-created`; следующие
запуски installer пропускают этот шаг. Если `admin` создан вручную, installer
не умеет обнаружить это заранее: используйте существующую учётку и не добавляйте
флаг создания. Ошибка/отмена ввода пароля не останавливает запущенный портал;
показанную команду можно выполнить отдельно. Данные установки находятся вне Git;
описание файлов и команд — в [инструкции launcher](../local/README.md).

План и проверка ничего не устанавливают, не запускают Docker Desktop и не создают
workspace:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\install.ps1 -Workspace "C:\Portal work\demo" -PortalSource "C:\src\test-portal" -Plan
# Для проверки текущих зависимостей замените -Plan на -CheckOnly.
```

```bash
bash installer/install.sh --workspace "$HOME/portal work/demo" --portal-source "$HOME/src/test-portal" --dry-run
# Для проверки текущих зависимостей замените --dry-run на --check.
```

Код 0 означает успех; в режиме проверки код 1 также означает недостающую
зависимость/недоступный daemon. Ошибки установки сопровождаются сообщением.
Docker daemon ожидается ограниченное время (около 120–180 секунд с учётом проб),
затем HTTP `/api/health` — до 180 секунд. После тайм-аута данные сохраняются;
проверьте `logs` launcher и повторите bootstrap. Обычные Docker probe-команды
ограничены тайм-аутом; загрузка пакетов и сборка Docker зависят от скорости сети.

Агент, исполнитель тестов и тестовый стенд в эту установку пока не входят.
Docker socket в контейнер портала не передаётся.

Проверки кода без установки системных пакетов:

```bash
python -m unittest discover -s installer -p "test_*.py" -v
python -m unittest discover -s local -p "test_*.py" -v
```

Тесты проверяют синтаксис Bash/PowerShell, read-only режимы и последовательность
bootstrap на подставных зависимостях. На текущем Windows хосте Docker отсутствует:
реальная установка на чистых Windows/Ubuntu/macOS и сборка/запуск контейнеров
ещё не проверены. Прошедшие mock-тесты не заменяют эту приёмку.

Официальные инструкции: [Docker Engine на Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
[Docker Desktop на Windows](https://docs.docker.com/desktop/setup/install/windows-install/),
[Docker Desktop на macOS](https://docs.docker.com/desktop/setup/install/mac-install/),
[WSL](https://learn.microsoft.com/en-us/windows/wsl/install),
[Homebrew](https://brew.sh/),
[актуальный cask Docker Desktop](https://formulae.brew.sh/cask/docker-desktop).
