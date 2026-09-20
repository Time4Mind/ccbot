# Автоматическое подключение ноды

Этот документ - операционный контракт для automation-агента. Он описывает
подключение worker-ноды к ccbot-лидеру без Telegram-настроек и без ручной
регистрации ноды в `state.json`.

Текущий основной интерфейс - локальная CLI-ручка на leader:

```bash
ccbot node bootstrap
```

Она создаёт короткоживущую подписанную pairing-команду. Агент запускает
полученную команду на целевом сервере по SSH, через KVN/VPN или другой
доступный remote shell. После первого health-сообщения leader сам создаёт
запись ноды и runtime. Telegram используется только для последующего выбора
ноды, сессий и удаления ноды; создание ноды через UI не является частью
основного flow.

## Роли и границы

| Роль | Где работает | Что делает |
| --- | --- | --- |
| relay | сервер со стабильным внешним IP | двунаправленно пересылает авторизованный transport между leader и worker |
| leader | основной ccbot | единственный Telegram poller, выдаёт bootstrap payload, хранит реестр нод |
| worker | целевая машина | запускает `ccbot-node-agent`, локально управляет tmux/Claude/Codex |

Telegram token нужен только leader. Worker его не получает и к Telegram API не
обращается. Доступ worker к Claude/Codex и к VPN провайдеров остаётся локальным
для worker. Relay переносит control-plane и контекстные RPC, а не заменяет
провайдерский VPN.

## Контракт прямой ручки

### Вход

На leader должны быть доступны только transport-параметры:

```text
CCBOT_NODE_RELAY_URL=relay.example.net:8765
CCBOT_NODE_LEADER_ID=local
CCBOT_NODE_SECRET=<leader-secret>
```

Их можно передать окружением или положить в `./.env` либо
`$CCBOT_DIR/.env`. `ccbot node bootstrap` читает эти файлы напрямую и не
проверяет `TELEGRAM_BOT_TOKEN` и `ALLOWED_USERS`.

Не передавай `CCBOT_NODE_SECRET` аргументом командной строки: он может попасть
в process list. Секрет должен оставаться в окружении/`.env` сервиса.

Опционально можно задать `CCBOT_NODE_PAIRING_TTL` в секундах. По умолчанию
одноразовый payload живёт 600 секунд.

### Вызов

Для стабильного worker id и имени:

```bash
ccbot node bootstrap --node-id worker1 --name "Worker 1"
```

По умолчанию выводится один JSON-объект. Если `--node-id` не передан, leader
сам создаёт `worker-<random>` и включает его и в pairing token, и в команду:

```json
{
  "command": "uv run ccbot-node-agent --pairing '<one-time-pairing-link>' --node-id worker1 --name 'Worker 1'",
  "display_name": "Worker 1",
  "expires_at": 0,
  "leader_id": "local",
  "node_id": "worker1",
  "relay_url": "relay.example.net:8765"
}
```

`expires_at` в реальном ответе - Unix timestamp, а в примере заменён на `0`.
Поле `command` содержит bearer-подобный одноразовый pairing token, связанный с
конкретным `node_id`. После первого подключения relay выдаёт процессу worker
отдельный node-specific credential для reconnect; он не печатается в receipt и
сохраняется на worker в `~/.ccbot-worker/node-credential.json` с правами `0600`.
Путь можно изменить через `CCBOT_NODE_CREDENTIAL_FILE`.
Агент
может разобрать JSON и выполнить только это поле на worker. Для режима, где
нужен только shell command:

```bash
ccbot node bootstrap --node-id worker1 --name "Worker 1" --format command
```

Успех ручки означает только выпуск payload: exit code `0` и валидный JSON или
command. Это ещё не означает, что worker подключён.

### Выполнение на worker

Агент должен выполнить ровно значение поля `command` в корне checkout ccbot,
где доступны `uv` и зависимости проекта:

```bash
uv run ccbot-node-agent --pairing '<one-time-pairing-link>' --node-id worker1 --name 'Worker 1'
```

Если в payload уже есть `--node-id` и `--name`, их нельзя переписывать: pairing
token принимает только этот `node_id`. Процесс должен оставаться запущенным; для
постоянной работы агент размещает его под уже принятой на сервере supervisor-
схемой (`systemd`, launchd, tmux или эквивалент). Сам bootstrap не устанавливает
ОС-пакеты и не создаёт service unit.

При рестарте `ccbot-node-agent` использует сохранённый reconnect-credential.
Созданные им tmux-окна содержат только служебные `@ccbot_session_id` и
`@ccbot_backend`, поэтому управление такими сессиями восстанавливается без
перезапуска агентов. Очередь Telegram-запросов при этом не восстанавливается.

При успешном первом соединении worker печатает безопасный receipt:

```text
ccbot-node-agent: connected
node_id=worker1
display_name=Worker 1
leader_id=local
relay_url=relay.example.net:8765
```

Секрет и полный pairing link в receipt не печатаются. Receipt подтверждает
транспортное соединение worker с relay, но ещё не готовность backend.

## Полный алгоритм automation-агента

1. Определи leader, relay, целевой сервер, checkout ccbot и нужные worker
   backends. Для relay используй сервер со стабильным внешним IP; leader и
   worker могут находиться за NAT.
2. Проверь, что relay запущен и слушает адрес, указанный в
   `CCBOT_NODE_RELAY_URL`.
3. На leader вызови `ccbot node bootstrap --format json`. Не изобретай pairing
   link и не извлекай секрет из логов.
4. Проверь `expires_at` и передай ровно значение `command` на целевой сервер.
   Не печатай его в отчёты, chat или постоянные логи.
5. Запусти команду на worker и дождись строки
   `ccbot-node-agent: connected`.
6. Дождись health-события на leader. В `state.json` должна появиться нода с
   тем же `node_id`; её состояние должно перейти в `ready`, когда worker
   сообщил хотя бы один доступный backend.
7. Проверь фактический сценарий: нода видна в меню нод, доступен нужный
   backend, на ней можно создать/выбрать сессию. Только это является
   завершением подключения.

## Краткие разрывы связи

Если выбранная нода временно недоступна, новые запросы к уже выбранной сессии
попадают в FIFO этой сессии. Для каждого запроса Telegram показывает позицию и
статус. Очередь ограничена 10 запросами и живёт не более 15 минут: после
восстановления запросы передаются строго по порядку, после TTL помечаются как
неотправленные. Очередь существует только в памяти leader и намеренно не
переживает его рестарт.
При штатном restart/shutdown каждый ожидающий Telegram-receipt перед остановкой
меняется на `❌ Не отправлено в сессию: ccbot перезапущен`. При аварийном
завершении процесса без shutdown-hook изменить старое сообщение невозможно без
сохранения очереди или идентификаторов сообщений.

Уведомления о длительном disconnect выключены по умолчанию. Их можно включить
в Settings -> Notifications -> Nodes: connection. Тогда ccbot сообщает только
о недоступности дольше минуты для выбранной ноды или ноды с активной сессией и
один раз о восстановлении; краткие сетевые сбои остаются тихими.

В меню Nodes действие «Отключить» запрещает новые запросы и выбор ноды, но
сохраняет ноду, её сессии и историю; её можно подключить обратно. «Удалить»
убирает ноду из реестра окончательно, сохраняет историю сессий и игнорирует
последующие health-сообщения от старого worker id.

Если worker receipt есть, а нода не стала `ready`, не объявляй подключение
завершённым: проверь health payload, наличие `claude`/`codex` в PATH и запуск
tmux. Если receipt нет, проверь relay address/port, TLS, срок действия payload
и логи worker.

## Relay и TLS

На relay-сервере:

```bash
CCBOT_RELAY_CREDENTIALS='local=<leader-secret>' \
CCBOT_NODE_LEADER_ID=local \
uv run ccbot-node-relay
```

Для защищённого внешнего контура настрой сертификат relay через
`CCBOT_RELAY_TLS_CERT` и `CCBOT_RELAY_TLS_KEY`, а на leader используй
`CCBOT_NODE_RELAY_URL=tls://relay.example.net:8765` либо
`CCBOT_NODE_RELAY_TLS=true`. Pairing-команда унаследует TLS-схему из relay URL.

Не добавляй worker в `CCBOT_RELAY_CREDENTIALS` в основном flow. Подписанный
pairing token проверяется relay через leader secret; статический worker secret
нужен только для обратной совместимости с ручным legacy flow.

## Диагностика

| Симптом | Проверка | Действие |
| --- | --- | --- |
| `CCBOT_NODE_RELAY_URL is required` | окружение/`.env` leader | задать relay URL и повторить bootstrap |
| `CCBOT_NODE_SECRET is required` | окружение/`.env` leader | задать тот же secret, который указан в relay credentials |
| `pairing link has expired` | `expires_at` и время на worker | выпустить новый payload; старый не переиспользовать |
| `relay authentication failed` | leader id, secret, pairing link, relay credentials | не добавлять случайный worker secret; перевыпустить payload и проверить exact leader secret |
| timeout до receipt | DNS/порт/VPN/TLS на worker | проверить стабильный relay endpoint и сертификат |
| receipt есть, state `online`, но не `ready` | health backends и локальные binaries | установить/починить нужный backend и перезапустить worker |
| нода исчезла после перезапуска worker | supervisor и рабочая директория | запускать `ccbot-node-agent` как long-lived service из checkout |

## Удаление и отзыв

Создание и подключение выполняются прямой CLI-ручкой и automation-агентом.
Telegram-настройки не должны выдавать pairing-команду. В UI оставляется только
операция удаления ноды из реестра. Удаление не удаляет transcript и архивы
сессий автоматически и само по себе не останавливает уже запущенный worker.
Для полного транспортного отзыва останови worker и ротируй leader secret.

Pairing link является секретом с TTL 600 секунд. При утечке link выпусти новый
link и отзови старый штатным механизмом relay/leader; при необходимости
полностью ротируй `CCBOT_NODE_SECRET`, перезапусти relay и leader, затем
переподключи оставшиеся worker-ноды. Не сохраняй link в Git, state dump,
Telegram-карточках или обычных логах.

## Что не делает bootstrap

- не устанавливает Python, `uv`, tmux, Claude/Codex и VPN на target;
- не выдаёт worker Telegram token;
- не открывает произвольный shell через node API;
- не переносит файлы рабочей директории автоматически;
- не является доказательством готовности сессии: нужен health и проверка backend;
- не меняет `main`: разработка и проверка выполняются на
  `feature/multi-node-runtime`.

Ручной legacy-вариант с `CCBOT_NODE_ID`/`CCBOT_NODE_SECRET` на worker остаётся
совместимым, но не является инструкцией для automation-агента.
