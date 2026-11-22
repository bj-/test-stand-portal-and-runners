# test-stand-portal-and-runners

Переносимый комплект тестовых стендов, инструментов установки портала и runners.
Единый репозиторий под Apache-2.0. Код портала находится в `backend/` и `frontend/`.
Настройки установки, доступы и рабочие данные задаются во внешнем каталоге.

Стек: Python 3.12+, FastAPI, SQLAlchemy, Alembic, PostgreSQL, React, TypeScript и Docker Compose.
`local/` — launcher; `examples/http-smoke/` — нейтральный пример API и smoke runner.
Команды и тесты — README соответствующего компонента.

Состояние: `docs/STATUS.md`. План: `docs/plan.md`. Журнал: `docs/journal-YYYY-MM.md`.
