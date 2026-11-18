# test-stand-portal-and-runners

Переносимый комплект тестовых стендов, инструментов установки портала и runners.
Новый репозиторий с чистой историей и Apache-2.0. Код портала подключается из
отдельного checkout; проектные адаптеры и конфигурация клиента не входят в поставку.

Стек первой версии: Python 3.12, стандартная библиотека, Docker Compose v2.
`local/` — launcher; `examples/http-smoke/` — нейтральный пример API и smoke runner.
Команды и тесты — README соответствующего компонента.

Состояние: `docs/STATUS.md`. План: `docs/plan.md`. Журнал: `docs/journal-YYYY-MM.md`.
