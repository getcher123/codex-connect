# Проверенный контракт

## Совместимость

Legacy: расширение openai.chatgpt **26.1106.51428**, bundled codex-cli **0.167.0-alpha.4.1**, Linux backend в WSL, IPC snapshot **version 11**. Дата проверки: **2026-11-12**. Live сценарий: idle → native queue → completed response → idle.

Текущий перенесённый контракт: расширение **26.1111.21308**, CLI **0.169.0-alpha.11.1**, snapshot **13**. Native queue для него не разрешён; Windows + WSL использует отдельный [UI sender](windows-ui.md). Матрица подтверждена исходниками установленного адаптера; уровень проверки текущего пакета указан в корневом state.md. Неизвестные версии отклоняются.

Native Windows named pipe, macOS, незагруженные диалоги и отправка во время активного хода не подтверждены.

## Два транспорта

Расширение запускает app-server через stdin/stdout. Не читать чужой stdout и не запускать второй сервер для продолжения текущей IDE-сессии.

IPC расширения: CODEX_HOME/ipc/ipc.sock либо ~/.codex/ipc/ipc.sock. Формат: четыре байта длины uint32 little-endian, затем UTF-8 JSON. Это не JSONL app-server и не его control socket. Исходящий frame ограничен 8 MiB, входящий — 32 MiB; чтение идёт блоками до 64 KiB. Начальные connect/handshake/discovery/snapshot используют общий deadline, cleanup — отдельные 0.2 секунды суммарно.

## Чтение

1. Request initialize, version 0: уникальный requestId, sourceClientId=initializing-client, собственный clientType. Сохранить clientId успешного ответа.
2. На client-discovery-request отвечать client-discovery-response с тем же requestId и response.canHandle=false.
3. Собрать thread-stream-following-changed announcements как кандидатов. Retained/followed не означает визуальный фокус.
4. Request thread-owner-discovery, version 1: params.hostId=local, params.conversationId. Взять handledByClientId успешного ответа как временный owner.
5. Адресный broadcast thread-stream-following-changed, version 1: targetClientIds с owner; params с hostId, conversationId, following=true.
6. Принимать thread-stream-state-changed только от нужного owner и для нужных host/thread. Требовать version 11 или 13 и change.type=snapshot.
7. После чтения отправить following=false и закрыть socket.

В conversationState проверить ID независимо от envelope. threadRuntimeStatus.type — состояние; requests — ожидающие запросы. Отсутствующий/некорректный requests не превращается в пустой список. Необязательный unconfirmedTurnSubmissions по проверенным схемам пуст по умолчанию; некорректный тип остаётся неизвестным. Summary содержит pending_state_known, pending_request_count и unconfirmed_submission_count.

Read/status проверяют полученный протокол без сканирования /proc и запуска CLI. Status не извлекает историю, но получает обычный snapshot. Doctor отдельно проверяет активный backend и ограниченный handshake; healthy не доказывает owner/idle конкретного диалога. При поиске backend executable разрешается только после проверки argv на app-server.

observation.received_at_ms — время получения snapshot; freshness=received_in_current_request не является гарантией времени генерации источника. Read резервирует бюджет сначала новым ходам и сохраняет user message при большом количестве ответов. Обрезанные сообщения помечаются truncated.

Для paginated history turns может быть пустым: использовать turnHistory.history.entitiesByKey. В queued turn текст пользователя находится в items/userMessage/content, элементы type=text; params.input может отсутствовать. Ответы: items/agentMessage/text. Reasoning и tool output не возвращать.

Патчи имеют baseRevision/revision. Адаптер повторно читает snapshot; streaming reducer не нужен. Revision может меняться из-за самой рассылки и не доказывает выполнение нового сообщения.

## Legacy отправка

Только для строки матрицы с разрешённым native queue. В v13 send возвращает queue_transport_unsupported до запуска queue.

Использовать binary текущего расширения с argv: queue, --thread, UUID, --message, точный текст. shell=False, без --resume и без изменения модели/прав.

Перед send требовать, чтобы resolved socket совпал с resolved CODEX_HOME/ipc/ipc.sock. Native queue адресуется через CODEX_HOME; отдельный socket для него не подставлять. Для чтения другой socket допустим.

Перед subprocess фиксировать enqueue_started_at_ms. После отправки искать новый turn, отсутствовавший в исходном snapshot, с точным пользовательским текстом и started_at_ms не раньше этой границы; ответ брать из того же turn. Догруженная старая страница не подтверждает новую отправку. Неизвестное время или рассинхронизация часов могут оставить результат неподтверждённым. Queue item ID и userMessage.clientId — разные идентификаторы.

Не считать неоднозначную корреляцию успехом. При потере подтверждения не повторять отправку. Предварительный idle не блокирует параллельные действия человека.

## Windows + WSL UI

Следовать [windows-ui.md](windows-ui.md). Ввод — через обычный composer и один Submit. Требуются точные UUID/title/cwd, уникальная привязка имени, правильный WSL-контекст и отсутствие пользовательского черновика. Python lock координирует WSL-пользователя, Windows mutex — desktop-ввод между дистрибутивами. Они не блокируют человека.

UI коррелирует полный новый user turn по времени начала отправки; допускается только CRLF → LF. Обрезанный текст, старые/недатированные ходы или несколько совпадений не доказывают доставку. После timeout не переключать транспорт и не повторять отправку.

MSI bridge сохраняет caller operation ID и квитанцию до preflight. Исходный ID служит для восстановления; сам UI helper не предоставляет долговечного журнала. `refresh=false` читает квитанцию без IPC, `refresh=true` явно сверяет неизвестный исход. Установка нового checkout и перезапуск службы — отдельное развёртывание.

## Хранилища

state_5.sqlite открывать только mode=ro с PRAGMA query_only=ON для ограниченного поиска. Использовать актуальное name, если оно есть, затем title. Подтверждать адресата живым snapshot. Не читать credentials/config, не копировать history DB, не писать в SQLite/JSONL.

## Источники

- [Codex App Server](https://learn.chatgpt.com/docs/app-server)
- [Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
- [Codex IDE commands](https://learn.chatgpt.com/docs/ide/commands)

IPC описан по установленному коду и проверен с действующим владельцем. Bundles расширения, личные snapshots и переписки в репозитории не хранятся.

