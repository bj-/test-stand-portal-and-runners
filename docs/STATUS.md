# Статус проекта

_Последнее обновление: 2026-11-18_

## Цель / текущий фокус

Чистая переносимая поставка инструментов стендов, локального портала и runners.
Основа первой версии — изолированный launcher и нейтральный HTTP-smoke пример.
План: [этапы 0–7](plan.md). Разделение поставки — [ADR 0001](decisions/0001-clean-distribution.md).

## В работе

- Реализованы установщики Windows PowerShell и Ubuntu/macOS Bash: зависимости,
  init/up/health/admin-init/agent-init/agent-start, plan/check и продолжение после reboot.
- Подключён `stands.worker`: привязка токена к хосту, snapshot до Docker, heartbeat,
  стадии/логи и Allure (в том числе ошибочного smoke), retry прежних SHA с сохранением
  volumes. Реальная PostgreSQL/Docker-приёмка Windows прошла успех → намеренная ошибка
  → успешный повтор, сохранив контрольную таблицу, HEAD и dirty-файл. Фоновый
  start/reuse/stop через launcher и восстановление после HTTP503 приняты.
- Текущие проверки: portable 115 passed /218 подпроверок, backend 519 passed /4 skipped
  и 6 health-регрессий, три live-сценария PASS. Исправлены Windows Compose discovery,
  OS lock и readiness через venv redirector. Владелец разрешил commit/push всей
  переносимой поставки; публикация выполняется после проверки состава.
- По указанию владельца первый вход новой установки admin/admin без терминального
  ввода; предложение сменить пароль необязательное, отказ хранится в портале.
  admin-init проверяет БД при каждом запуске, существующие учётки и пароли не меняет.
  Проверки launcher/installer: 38 passed и 39 подпроверок; реальная проверка портала
  с PostgreSQL/Edge: 2 passed, backend: 212 passed / 4 skipped.
- Launcher принимает отдельный checkout портала и хранит данные вне исходников;
  повтор init сохраняет настройки, удалённый Docker endpoint отклоняется.
- Готов [скрипт развёртывания портала и учебного стенда](../DEPLOY.md): deploy.ps1 /
  deploy.sh, bootstrap зависимостей, run/stop, Compose build/deploy/migrate/seed/
  healthcheck/smoke. SQLite, runner/reporter, Allure HTML. Очередь подключена через worker;
  standalone deploy-demo сохраняет прежний прямой режим запуска.
- Первый шаг D: [контракт шаблона v1 и CLI](../stands/README.md), validate/prepare/replay,
  отдельные контексты из Git objects, полные SHA и frozen template; без изменения
  исходных clones и без fallback refs. Этот код входит в разрешённую поставку.
- Приёмка первого шага на Windows: 17 тестов контракта/Git + 112 подпроверок,
  отдельный CLI-сценарий и регрессия комплекта; всего 44 теста и 137 подпроверок.
  На первом шаге не было запуска Docker, lifecycle или интеграции с очередью.
- Приёмка продолжения: 71 тест и 193 подпроверки, все итогово прошли. Два первоначальных
  падения из-за незакрытых SQLite connections исправлены и перепроверены. Проверены
  реальные Git/HTTP/SQLite, wrappers и executor с mock Docker, а также настоящий
  Compose 2.39.4 config/ресурсный override без daemon. Запуск контейнеров не выполнялся.
- Повтор требует --retry, сохраняет snapshot и данные; результаты каждой попытки
  раздельны. execution.json хранит стадии, SHA, image IDs/digests и baseUrl.
  Публикация переносимого комплекта разрешена владельцем.
- Проверки кода: 26 тестов и 25 подпроверок прошли (launcher/health/example/bootstrap).
  Проверены native PowerShell 5.1/pwsh parsing, plan/check без изменений ОС,
  Git Bash syntax, mock bootstrap, реальные локальные HTTP-запросы. Найденные
  несовместимости Bash 3.2 и PowerShell 5.1 исправлены; независимое ревью завершено.

## Следующие шаги

- Приёмка установки с нуля на Windows/Ubuntu/macOS; Windows lifecycle уже принят.
- Подключить адаптер целевого приложения; учебный template-demo подготовлен.
- Обычные test-engine прогоны для динамических окружений. Мастер создания стенда
  реализован в `test-portal` (b3bd943); схема БД, реестр, API, очередь и worker подключены.

## Блокеры и открытые вопросы

- Docker установлен пользователем; локальные контейнеры портала/PostgreSQL работают,
  /api/health отвечает ok. Полный lifecycle учебного стенда подтверждён на отдельной
  Windows установке для приёмки; рабочая установка этой проверкой не менялась.
- Установка пакетов на чистых Windows/Ubuntu/macOS не запускалась; mock-проверки
  не подтверждают полную платформенную приёмку. Код подготовлен для этой проверки.
- Код портала подключается отдельно; доступ к нему не предоставляется этим репозиторием.

## Журнал

`docs/journal-YYYY-MM.md`.
