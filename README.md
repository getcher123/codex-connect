# Codex Connect

Skill для чтения сообщений и отправки текста в **уже работающий Codex в VS Code**.

Чтение использует локальный IPC владельца диалога. Для Windows + WSL добавлен переносимый отправитель через обычный интерфейс Codex; встроенный `codex queue` остаётся только для проверенной legacy-версии. Второй app-server, внешняя БД, сетевой сервис и отдельный API-ключ не нужны.

## Возможности и граница

- Найти доступные диалоги или разрешить точное название в ID.
- Прочитать последние пользовательские и ассистентские сообщения.
- Узнать состояние и наличие ожидающего вопроса.
- Передать текст в свободный диалог и проверить новый ход с ответом.
- Вернуть JSON для вызывающего агента.

Адресат выбирается явно. Skill не определяет визуальный фокус VS Code и не считает самую свежую сессию текущей.

Поддержка: **Linux/WSL, Python 3.10+, стандартная библиотека**. Native Windows named pipe и macOS не реализованы.

| Расширение / bundled CLI | IPC | Отправка | Проверка |
| --- | --- | --- | --- |
| 26.1106.51428 / 0.167.0-alpha.4.1 | snapshot 11 | Legacy native queue | Живой обмен 2026-11-12; регрессии сохранены |
| 26.1111.21308 / 0.169.0-alpha.11.1 | snapshot 13 | Windows + WSL UI sender; native queue запрещён | Перенесён установленный контракт; текущие результаты в [state.md](state.md) |

UI sender требует интерактивный рабочий стол Windows, Windows PowerShell, `wslpath`, текущий `WSL_DISTRO_NAME` и стандартный профиль `~/.codex`. Нестандартные профили для UI не подтверждены и отклоняются.

В Windows + WSL запускать Python внутри того же дистрибутива и от того же пользователя, где работает расширение. Внутренний IPC может измениться: чтение проверяет версию snapshot; legacy native send дополнительно проверяет установленную версию backend. UI sender требует snapshot 13 и проверки адресата/окна, но не устанавливает точную версию UI-сборки; ограничения описаны в [windows-ui.md](skills/codex-connect/references/windows-ui.md). Проверка idle не является атомарной блокировкой параллельных действий человека в IDE.

## Запуск из репозитория

```bash
python3 skills/codex-connect/scripts/codex_connect.py doctor
python3 skills/codex-connect/scripts/codex_connect.py discover
```

Агенту передать [SKILL.md](skills/codex-connect/SKILL.md). Общие параметры `--codex-home`, `--socket`, `--codex-bin`, `--timeout` размещать **до подкоманды**.

Если нужного диалога нет, открыть его в расширении и повторить discovery; второй backend не запускать.

### Пример для отдельного тестового диалога

Сначала создать или выбрать разрешённый тестовый диалог **Codex Connect Smoke**. Последний вызов ниже действительно отправляет сообщение и разрешён только для legacy-версии из матрицы:

```bash
python3 skills/codex-connect/scripts/codex_connect.py status --title "Codex Connect Smoke"
python3 skills/codex-connect/scripts/codex_connect.py read --title "Codex Connect Smoke"
python3 skills/codex-connect/scripts/codex_connect.py send --title "Codex Connect Smoke" --message "This is a connection test. Do not use tools or access files. Reply exactly CODEX_CONNECT_OK." --wait 30
```

Для произвольного текста предпочесть `--message-file`. Не вставлять текст пользователя в PowerShell/Bash через конкатенацию. После timeout сначала выяснить исход и прочитать сессию; автоматического повтора отправки нет.

### Windows + WSL и восстановление

Для нового UI sender следовать [windows-ui.md](skills/codex-connect/references/windows-ui.md). Скрипт использует соседние файлы, определяет пути через `wslpath`, учитывает все верхнеуровневые окна Code и проверяет WSL-контекст. Существующий пользовательский черновик и неоднозначный выбор окна блокируют ввод.

Через [MSI Remote Agent](https://github.com/getcher123/MSI-remote-agent) контроллер сначала сохраняет свой `operation_id`, затем передаёт его в `codex_send_message`. Совместимый bridge создаёт квитанцию **до первого preflight-чтения**. После потери ответа читать `codex_delivery_status` по исходному ID; не запускать отправку заново. UI-скрипт сам не заменяет сохраняемый журнал.

UI-ввод ограничен **32768 байтами UTF-8**. `staged_not_submitted` означает оставшийся черновик; `ui_invoked_unobserved` и `outcome_unknown` требуют сверки без повтора. UI-корреляция допускает только CRLF → LF: не удаляет пробелы, обратные слеши или часть сообщения. Доставка не означает завершение поручения.

`status` возвращает метаданные без извлечения истории, но получает обычный IPC snapshot. `read` и `status` не запускают CLI version probe. Общий начальный IPC deadline — 1–60 секунд; cleanup имеет отдельный общий бюджет 0.2 секунды. Входящий frame ограничен 32 MiB, вывод сообщений — отдельно.

Неизвестный pending не превращается в пустой список. Для отправки нужны `idle`, `pending_state_known=true` и нулевые `pending_request_count`/`unconfirmed_submission_count`. `observation.received_at_ms` — время получения snapshot, не генерации источника. Положительный doctor подтверждает backend и IPC handshake, но не готовность конкретного диалога.

### Значение результата

| phase | Значение |
| --- | --- |
| queued | CLI подтвердил постановку; ответ ещё не подтверждён |
| observed | Найден новый ход с точным пользовательским текстом |
| completed | Этот ход завершён и содержит ответ ассистента |
| ambiguous | Найдено несколько подходящих новых ходов |
| outcome_unknown | Исход queue-вызова не подтверждён; повтор небезопасен |

Сохранить queue_item_id, baseline_turn_ids, message_sha256 и enqueue_started_at_ms из результата для последующего разбора. Не считать exit code 0 доказательством ответа: у фаз queued/observed обмен ещё может продолжаться.

Для send socket обязан совпадать с разрешённым путём CODEX_HOME/ipc/ipc.sock: проверка одного экземпляра и отправка через другой запрещены. Произвольный --socket разрешён только для чтения. Подтверждение ответа требует нового хода со временем начала не раньше отправки; при неизвестном времени или рассинхронизации часов результат остаётся неподтверждённым.

## Установка в локальный Codex

Из корня checkout создать ссылку в пользовательском каталоге skills Linux/WSL:

```bash
python3 - <<'PY'
from pathlib import Path
source = (Path.cwd() / "skills" / "codex-connect").resolve()
if not (source / "SKILL.md").is_file():
    raise SystemExit("Run from the codex-connect repository root")
destination = Path.home() / ".agents" / "skills" / "codex-connect"
destination.parent.mkdir(parents=True, exist_ok=True)
if destination.exists() or destination.is_symlink():
    raise SystemExit(f"Destination already exists: {destination}; inspect before updating")
destination.symlink_to(source, target_is_directory=True)
print(destination)
PY
```

Затем вызвать `$codex-connect`. Расположение skills и поддержка символических ссылок описаны в [официальной документации](https://learn.chatgpt.com/docs/build-skills). Эта инструкция устанавливает ссылку; соединение возникает только при запуске скрипта. Для ChatGPT с Remote Desktop Commander использовать отдельный [маршрут WSL](skills/codex-connect/references/remote-desktop.md).

## Структура

| Путь | Назначение |
| --- | --- |
| `skills/codex-connect/SKILL.md` | Маршрут агента |
| `skills/codex-connect/scripts/codex_connect.py` | IPC, состояние и legacy queue |
| `skills/codex-connect/scripts/send_ui.py` | WSL preflight и наблюдение UI-доставки |
| `skills/codex-connect/scripts/send_ui.ps1` | Окно, composer и единственный Submit |
| `skills/codex-connect/references/` | Протокол и удалённый запуск |
| `tests/` | Изолированные тесты |
| `AGENTS.md` | Правила сопровождения |
| `state.md` | Подтверждённые результаты и ограничения |

## Проверка

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/codex-connect/scripts/codex_connect.py skills/codex-connect/scripts/send_ui.py
```

В Windows PowerShell выполнить `tests/test_send_ui_windows.ps1`: fixture вызывает чистые guard-функции без перечисления окон и ввода текста. CI проверяет Python на Linux и эти guards на Windows. Обновление GitHub не изменяет установленный агент.

Офлайн-тесты не подключаются к пользовательскому Codex и не вызывают модель. Живой smoke выполнять только в разрешённом пользователем диалоге. Результаты — в [state.md](state.md).

