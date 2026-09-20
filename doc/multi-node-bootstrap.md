# Автоматическое подключение ноды

Этот документ - операционный контракт для automation-агента. Он описывает
подключение worker-ноды к ccbot-лидеру без Telegram-настроек и без ручной
регистрации ноды в `state.json`.

Текущий основной интерфейс - локальная CLI-ручка на leader:

```bash
ccbot node bootstrap
```

Она создаёт короткоживущую подписанную pairing-команду. Агент запускает
полученную команду на целевом сервере по SSH, через VPN или другой
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
  "command": "uv run ccbot-node-agent --pairing '<one-time-pairing-link>' --node-id worker1 --name 'Worker 1' --install-service",
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
Путь можно изменить через `CCBOT_NODE_CREDENTIAL_FILE`. Команда по умолчанию
включает `--install-service`: она обменивает token на reconnect-credential,
устанавливает user-service через launchd/systemd, запускает его и проверяет
supervisor status. Pairing token в service-файл не попадает. Агент может
разобрать JSON и выполнить только это поле на worker. Для режима, где
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
uv run ccbot-node-agent --pairing '<one-time-pairing-link>' --node-id worker1 --name 'Worker 1' --install-service
```

Если в payload уже есть `--node-id` и `--name`, их нельзя переписывать: pairing
token принимает только этот `node_id`. После receipt
`ccbot-node-agent: service active` процесс находится под user-level supervisor
(`systemd --user` на Linux или LaunchAgent на macOS). Bootstrap не устанавливает
ОС-пакеты и требует уже установленный ccbot.

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
статус. Очередь ограничена 10 запросами на сессию и 100 запросами суммарно и
живёт не более 15 минут: после
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
сначала отзывает credential на relay и разрывает соединение, затем убирает ноду
из реестра. Сессии и история сохраняются. Revocation хранится relay в
`~/.ccbot-relay/revoked-nodes.json` или `CCBOT_RELAY_REVOCATIONS_FILE`.

Remote-сессии используют тот же Telegram control surface: Escape, завершение
сессии, capture интерактивного prompt и navigation keys передаются worker через
RPC. Перенос контекста создаёт и выбирает новую сессию, но не архивирует
исходную - пользователь закрывает её отдельно.

Worker не вводит собственного лимита на число сессий. Новые сессии
создаются, пока это позволяют реальные ресурсы операционной системы. Worker
по-прежнему сообщает `active_sessions` в health payload; `max_sessions: 0`
означает отсутствие лимита ccbot.

При запуске Codex leader и worker используют один startup lifecycle. Если Codex
показывает update prompt, ccbot выбирает `Update now`, дожидается выхода
updater и один раз повторяет исходную команду с теми же flags,
directory, resume/context intent и session name. Повторный update prompt,
timeout, cancellation
или ошибка startup завершаются контролируемо: созданное tmux-окно
удаляется и не считается живой сессией.

## Автообновление worker

Worker сообщает в health точный Git commit запущенного ccbot. После обновления
и перезапуска leader сравнивает этот commit со своим и через уже
аутентифицированный relay отправляет worker команду перейти на тот же SHA.
Worker делает `git fetch origin`, проверяет наличие именно этого commit,
переходит в detached HEAD и перезапускает только `ccbot-node-agent`. Живые
tmux-сессии продолжают работать.

Обновление не запускается, если leader работает не из чистого Git commit,
worker имеет локальные изменения отслеживаемых файлов или commit недоступен в
его `origin`. Повторная попытка ограничена интервалом в пять минут. Неизвестные
и произвольные refs не принимаются - только полный 40-символьный SHA.

Если в целевой ревизии отслеживается `uv.lock`, worker перед перезапуском
выполняет `uv sync --frozen`; при ошибке возвращает checkout на прежний commit
и остаётся на старом процессе. Без отслеживаемого lock-файла зависимости
автоматически не переразрешаются. Для нестандартного расположения checkout
можно задать `CCBOT_NODE_REPO_DIR`.

## Полный SSH-доступ с leader

Для административных и агентских задач используется штатный OpenSSH, а не
shell-RPC внутри ccbot relay. Подключение выполняется под тем же OS-пользователем,
который запускает `ccbot-node-agent`: доступны его файлы, процессы и команды.
Если этому пользователю уже разрешён `sudo`, он работает и через SSH; ccbot не
добавляет привилегии и не меняет `sudoers`.

Worker публикует в health только несекретный маршрут:

```bash
CCBOT_NODE_SSH_HOST=127.0.0.1
CCBOT_NODE_SSH_USER=worker-user
CCBOT_NODE_SSH_PORT=22041
CCBOT_NODE_SSH_PROXY_JUMP=ccbot-bastion
```

Эти переменные нужно задать при установке node-agent service. Leader сохраняет
их в реестре нод, после чего локальный агент может открыть интерактивную сессию
или выполнить команду:

```bash
ccbot node ssh worker1
ccbot node ssh worker1 -- id
ccbot node ssh worker1 -- sudo systemctl status ccbot-node-agent
```

Чтобы маршрут сразу попал в одноразовую bootstrap-команду и затем в service
environment worker:

```bash
ccbot node bootstrap \
  --node-id worker1 \
  --name "Worker 1" \
  --ssh-host 127.0.0.1 \
  --ssh-user worker-user \
  --ssh-port 22041 \
  --ssh-proxy-jump ccbot-bastion
```

Команда запускает системный `ssh` без shell-прослойки. Приватные ключи,
пароли и SSH-agent sockets не передаются через relay и не записываются в
`state.json`; их продолжает обслуживать обычный `~/.ssh/config`/SSH agent
leader-машины.

### Worker без входящего публичного адреса

Минимальный полностью self-hosted вариант - обратный SSH-туннель через свой
bastion (им может быть сервер рядом с ccbot relay). Worker устанавливает
исходящее соединение:

```bash
ssh -NT \
  -o BatchMode=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:22041:127.0.0.1:22 \
  ccbot-tunnel@bastion.example
```

Порт привязывается только к loopback bastion, поэтому в интернет не
публикуется. На leader в `~/.ssh/config` создаётся alias `ccbot-bastion`, а в
worker service задаются четыре переменные выше. Для каждой ноды нужен отдельный
reverse port. Туннель следует запускать отдельным `systemd --user`/launchd
service с restart policy; его жизненный цикл не зависит от Telegram и relay.

На worker публичный ключ leader добавляется в `authorized_keys` именно того
пользователя, под которым должен работать удалённый агент. Отдельный ключ
worker→bastion рекомендуется ограничить на bastion только TCP-forwarding и
конкретным reverse port. Проверку host key отключать нельзя: fingerprints
bastion и worker предварительно заносятся в `known_hosts` leader/worker.

Если уже есть собственный WireGuard/VPN, reverse tunnel не нужен:
`CCBOT_NODE_SSH_HOST` указывает на приватный VPN-адрес worker,
`CCBOT_NODE_SSH_PORT=22`, а `CCBOT_NODE_SSH_PROXY_JUMP` остаётся пустым.

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
| worker не обновляется вслед за leader | `ccbot_version` в health, чистота обоих checkout и доступность SHA в `origin` | убрать tracked-изменения либо отправить commit в remote; проверить warning `node auto-update` |

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
- не устанавливает и не настраивает sshd, WireGuard или bastion автоматически;
- не переносит файлы рабочей директории автоматически;
- не является доказательством готовности сессии: нужен health и проверка backend;
- не меняет `main`: разработка и проверка выполняются на
  `feature/multi-node-runtime`.

Ручной legacy-вариант с `CCBOT_NODE_ID`/`CCBOT_NODE_SECRET` на worker остаётся
совместимым, но не является инструкцией для automation-агента.
