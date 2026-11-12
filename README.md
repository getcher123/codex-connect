# Codex Connect

Skill для чтения сообщений и отправки текста в **уже работающий Codex в VS Code**.

Отправка использует встроенный `codex queue`, чтение — локальный IPC владельца диалога. Второй app-server, внешняя БД, сетевой сервис и отдельный API-ключ не нужны.

## Возможности и граница

- Найти доступные диалоги или разрешить точное название в ID.
- Прочитать последние пользовательские и ассистентские сообщения.
- Узнать состояние и наличие ожидающего вопроса.
- Передать текст в свободный диалог и проверить новый ход с ответом.
- Вернуть JSON для вызывающего агента.

Адресат выбирается явно. Skill не определяет визуальный фокус VS Code и не считает самую свежую сессию текущей.

Поддержка: **Linux/WSL, Python 3.10+, стандартная библиотека**. Проверенная версия расширения — **26.1106.51428**, bundled CLI — **0.167.0-alpha.4.1**, snapshot protocol — **11**. Native Windows named pipe и macOS не реализованы.

В Windows + WSL запускать Python внутри того же дистрибутива и от того же пользователя, где работает расширение. Внутренний IPC может измениться: чтение проверяет версию snapshot, отправка дополнительно проверяет установленную версию backend. Проверка idle не является атомарной блокировкой параллельных действий человека в IDE.

## Запуск из репозитория

```bash
python3 skills/codex-connect/scripts/codex_connect.py doctor
python3 skills/codex-connect/scripts/codex_connect.py discover
```

Агенту передать [SKILL.md](skills/codex-connect/SKILL.md). Общие параметры `--codex-home`, `--socket`, `--codex-bin`, `--timeout` размещать **до подкоманды**.

Если нужного диалога нет, открыть его в расширении и повторить discovery; второй backend не запускать.

### Пример для отдельного тестового диалога

Сначала создать или выбрать в расширении тестовый диалог с названием **Codex Connect Smoke**. Второй вызов ниже действительно отправляет сообщение:

```bash
python3 skills/codex-connect/scripts/codex_connect.py read --title "Codex Connect Smoke"
python3 skills/codex-connect/scripts/codex_connect.py send --title "Codex Connect Smoke" --message "This is a connection test. Do not use tools or access files. Reply exactly CODEX_CONNECT_OK." --wait 30
```

Для произвольного текста предпочесть `--message-file`. Не вставлять текст пользователя в PowerShell/Bash через конкатенацию. После timeout сначала выяснить исход и прочитать сессию; автоматического повтора отправки нет.

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
| `skills/codex-connect/scripts/codex_connect.py` | Единственный адаптер |
| `skills/codex-connect/references/` | Протокол и удалённый запуск |
| `tests/` | Изолированные тесты |
| `AGENTS.md` | Правила сопровождения |
| `state.md` | Подтверждённые результаты и ограничения |

## Проверка

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile skills/codex-connect/scripts/codex_connect.py
```

Офлайн-тесты не подключаются к пользовательскому Codex и не вызывают модель. Живой smoke выполнять только в разрешённом пользователем диалоге. Результаты — в [state.md](state.md).

