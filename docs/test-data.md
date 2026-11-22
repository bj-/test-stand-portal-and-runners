# Тестовые данные

Наборы данных принадлежат проекту и хранятся в БД портала. Их неизменяемые версии можно вручную применять к разным стендам либо необязательно подключать к стенду для автоматического применения. Существующий seed/reset и описание базового состава стенда продолжают работать.

Перед включением установите адаптер на stand-host и настройте команды `test_data.<target>.apply/inspect` в manifest установки. Сам портал не подключается к БД стенда. Пример [projects.example.yaml](../projects.example.yaml), протокол и SQL-ограничения — [agent/README.md](../agent/README.md). Добавление конфигурации требует обычного обновления конфигурации установки и наличия подходящего агента; этот документ не означает выполненный деплой.

## Набор, версия, цель и привязка

`key` уникален внутри проекта. `format` задаёт совместимость с адаптером, например `relational-v1`. JSON данных — объект до 2 MiB UTF-8, без NaN/Infinity. SQL-адаптер выделяет дополнительно 64 KiB для файлового envelope.

Изменение данных создаёт новую версию; старые версии сохраняются. Версия применения выбирается явно. Выпуск версии 2 не меняет привязку, в которой зафиксирована версия 1. Один набор можно использовать на нескольких стендах и целях с совместимым format. Цель (`primary`, `analytics` и т. п.) — доверенная конфигурация команды/подключения установки; браузер не задаёт DSN, путь или shell-команду.

Привязка необязательна: «Залить сейчас» создаёт разовую задачу без неё. Привязка содержит dataset_id, version, target, enabled, auto_apply, position. Один набор подключается к одной цели стенда один раз; версию меняют в этой привязке. Её отключение/удаление не удаляет данные из БД стенда и не отменяет прошлое применение. Наборы доступны совместно ролям admin/tester/developer установки; отдельной проверки членства в проекте в портале нет. Developer создаёт наборы и версии своих наборов, применяет и просматривает данные; admin/tester также редактируют чужие наборы и управляют привязками. Guest/viewer/llm доступа к payload/result не имеют. Роль agent не получает пользовательский API каталога и просмотра; назначенному исполнителю payload передаётся только вместе с его задачей, результат он возвращает через служебный endpoint.

## Автоматическое применение и ошибки

При постановке deploy/reset/seed портал фиксирует снимок включённых привязок с auto_apply и их конкретных версий, упорядоченный по `(position,id)`. Последующее изменение привязки не меняет уже поставленную задачу. Порядок исполнения: базовая команда стенда → адаптеры по снимку → завершение задачи → существующий smoke для deploy/reset. Для restart сохраняется прежний smoke без повторного посева; seed не добавляет smoke. Ошибка адаптера останавливает цепочку и не допускает успешного завершения/smoke этой цепочки.

SQL-адаптер использует одну транзакцию для всех таблиц одного набора. Несколько привязок/целей не образуют общую транзакцию: предшествующие успешные применения и базовая команда не откатываются при ошибке следующего адаптера. Проверьте закрытый результат, исправьте причину и повторите. Отмена/timeout завершает группу процессов адаптера. При обрыве после COMMIT до доставки результата возможен неопределённый исход; inspect помогает проверить состояние.

## API

Ниже команды для bash с curl; токен пользовательской роли берётся из окружения. Не включайте трассировку shell/curl и не сохраняйте токен в репозитории. Примеры используют условные ID: подставляйте ID из ответа.

```bash
export TEST_DATA_PORTAL_URL='https://portal.example'
export TEST_DATA_PROJECT='ACME'
export TEST_DATA_STAND='dev'
# TEST_DATA_API_TOKEN уже задан защищённым окружением установки/пользователя.
BASE="$TEST_DATA_PORTAL_URL/api/projects/$TEST_DATA_PROJECT"

# Создать набор и первую версию.
curl --fail-with-body -sS "$BASE/datasets" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"key":"sample-users","title":"Пример пользователей","description":"Без данных клиента","format":"relational-v1","data":{"tables":[{"table":"users","key":["id"],"rows":[{"id":-101,"name":"Example"}]}]}}'

# Каталог, карточка набора и конкретная версия.
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/datasets"
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/datasets/1"
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/datasets/1/versions/1"

# JSON данных без метаданных: подходит для сохранения и повторной загрузки.
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/datasets/1/versions/1/content"

# Опубликовать новую версию; привязки остаются на прежней версии.
curl --fail-with-body -sS "$BASE/datasets/1/versions" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"data":{"tables":[{"table":"users","key":["id"],"rows":[{"id":-101,"name":"Example v2"}]}]}}'

# Доступные цели и существующие привязки.
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" \
  "$BASE/stands/$TEST_DATA_STAND/test-data"

# Разовое применение версии 1. Ответ {"id":...} — ID асинхронной задачи.
curl --fail-with-body -sS "$BASE/stands/$TEST_DATA_STAND/test-data/apply" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"dataset_id":1,"version":1,"target":"primary"}'

# Необязательная привязка для deploy/reset/seed (admin/tester).
curl --fail-with-body -sS "$BASE/stands/$TEST_DATA_STAND/test-data/bindings" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"dataset_id":1,"version":1,"target":"primary","enabled":true,"auto_apply":true,"position":10}'

# Смена версии привязки: PUT передаёт все её поля.
curl --fail-with-body -sS -X PUT "$BASE/stands/$TEST_DATA_STAND/test-data/bindings/1" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"dataset_id":1,"version":2,"target":"primary","enabled":true,"auto_apply":true,"position":10}'

# Просмотр фактических данных тоже создаёт задачу.
curl --fail-with-body -sS "$BASE/stands/$TEST_DATA_STAND/test-data/inspect" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data '{"target":"primary","query":{"table":"users","where":{"id":-101},"columns":["id","name"],"limit":100}}'

# Повторяйте чтение до конечного статуса задачи, подставив ID ответа.
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/test-data/jobs/42"

# Исходный JSON результата без преобразования длинных числовых ID браузером.
curl --fail-with-body -sS -H "Authorization: Bearer $TEST_DATA_API_TOKEN" "$BASE/test-data/jobs/42/result"

# Удалить привязку, сохранив фактические данные стенда.
curl --fail-with-body -sS -X DELETE -H "Authorization: Bearer $TEST_DATA_API_TOKEN" \
  "$BASE/stands/$TEST_DATA_STAND/test-data/bindings/1"
```

Создание набора/версии/привязки/задачи возвращает 201, удаление привязки — 204. Результат задачи имеет поля `id,status,message,result`; result доступен только закрытым пользовательским ролям при done/failed. Для базового действия с автопосевом result содержит массив operations с целью, версией и результатом каждого этапа; полезная нагрузка в него не включается. Структурированная ошибка адаптера также возвращается закрыто. Ответ общей карточки задачи и общие логи не содержат payload/result. `/result` возвращает 409, если результат отсутствует.

401 означает отсутствие входа, 403 — недостаток прав, 404 — отсутствие ресурса в выбранном проекте, 409 — конфликт ключа/привязки или неподходящее состояние прогона, 422 — неверный JSON, размер, формат либо конфигурацию цели. Успешная постановка ещё не означает успешного применения. При потере ответа или серверной ошибке исход записи может быть неизвестен: сначала перечитайте каталог/задачу, не публикуйте автоматически дубликат версии. API не обещает однократность POST при повторе запроса.

UI загружает и отправляет исходный JSON-текст, не пересериализуя числовые ID через JavaScript; просмотр, копирование и скачивание также используют `/content` и `/result`. Лимит редактора/файла — 2 MiB вместе с пробелами, сервер проверяет компактное представление. Десятичные значения, для которых нужна точность сверх обычного JSON number, передавайте строкой по контракту адаптера.

## Из выполняющегося теста

Агент передаёт тесту `TEST_DATA_PARENT_JOB_ID`, `TEST_DATA_PROJECT`, `TEST_DATA_STAND`, `TEST_DATA_PORTAL_URL`. `TEST_DATA_API_TOKEN` нужно отдельно настроить в защищённом окружении прогона (для стандартного агента — файл проекта в `AGENT_ENV_DIR`); используйте пользовательский токен tester/developer с нужным доступом. Его нельзя заменять `AGENT_TOKEN`: техническая роль agent не имеет пользовательского API данных. Для developer имя API-токена должно совпадать с `created_by` родительского прогона; tester обслуживает прогоны установки согласно своей роли.

```bash
BASE="$TEST_DATA_PORTAL_URL/api/projects/$TEST_DATA_PROJECT"
curl --fail-with-body -sS "$BASE/stands/$TEST_DATA_STAND/test-data/apply" \
  -H "Authorization: Bearer $TEST_DATA_API_TOKEN" -H 'Content-Type: application/json' \
  --data "{\"dataset_id\":1,\"version\":1,\"target\":\"primary\",\"parent_job_id\":$TEST_DATA_PARENT_JOB_ID}"
```

Передайте `parent_job_id` также в inspect и дождитесь закрытого результата через `/test-data/jobs/{id}` до продолжения теста. Родитель должен быть running run того же стенда; developer может обслуживать только собственный прогон, admin/tester — прогон проекта. Дочерние операции сериализуются внутри блокировки родителя, включая совмещённый агент запуска тестов и stand-host. После окончания/отмены родителя ожидающие дочерние операции не выполняются. Без parent_job_id получается самостоятельная операция стенда, которая ждёт его блокировку; запрос из самого заблокировавшего стенд теста может поэтому ждать окончания родителя.

## Хранение и расширение

Данные наборов и версий находятся в БД **портала**, а не в Git и не только на стенде. Включите их в резервное копирование PostgreSQL портала вместе с метаданными привязок; ограничьте доступ и к резервным копиям. Это не резервная копия фактической БД стенда: её нужно обслуживать отдельно. Наборы не являются хранилищем паролей: используйте синтетические данные, не помещайте DSN/учётные данные в JSON. Секреты подключения остаются в окружении установки адаптера, токен API — в защищённом окружении пользователя/прогона.

Портал универсален за счёт версий, целей и адаптерного протокола. Предоставленный relational-v1 реализует SQLite/PostgreSQL. Для MongoDB, внешнего API или другого формата создайте отдельный адаптер с собственным format, валидацией, идемпотентностью и документированными гарантиями. Общая атомарность нескольких независимых хранилищ не предоставляется.
