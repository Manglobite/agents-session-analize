# OpenCode Session Evaluator — MVP

Локальный проект для анализа больших экспортированных агентских сессий OpenCode и отладки prompt-поведения оркестратора/сабагентов.

Цель — не выставить агенту абстрактную оценку, а построить компактную карту выполнения и ответить на практические вопросы:

1. где фактическая траектория выполнения разошлась с prompt-контрактом;
2. было ли правило/ограничение уже известно модели до ошибочного действия;
3. где возникала лишняя работа и повторные неуспешные действия;
4. как агент восстанавливался после ошибки;
5. где пользователь выступал внешним корректирующим контуром;
6. какие минимальные изменения prompt оркестратора и предоставленных сабагентов могут снизить вероятность повторения проблемы;
7. нужен ли дополнительный агент для отделимой роли, не покрытой существующей командой.

Тонкий OpenCode TypeScript-wrapper, строгие аргументы, основная алгоритмическая логика вынесена отдельно, shell-доступ агентам не требуется. Python запускается самим custom tool через `Bun.spawn`, аналогично подходу в `opencode-postgresql-readonly`.

## Архитектура

```text
OpenCode export JSON
        │
        ▼
session_eval_prepare       deterministic Python
        │
        ├── normalized events
        ├── semantic chunks
        └── manifest
                │
                ▼
      trace-summarizer     SLM, only large semantic chunks
                │
                ▼
session_eval_store_summary deterministic validation/persistence
                │
                ▼
session_eval_build_map     deterministic map + metrics/findings
                │
                ▼
      behavior-reviewer    strong LLM, one final review
                │
                ▼
session_eval_finalize      deterministic validation/report rendering
```

Оркестратор сам не читает огромный export, не суммаризирует его и не делает финальную оценку. Его задача — провести данные через фиксированный pipeline.

## Структура проекта

```text
.
├── opencode.jsonc
├── .gitignore
├── LICENSE
├── README.md
│
├── .opencode/
│   ├── agents/
│   │   ├── session-evaluator.md
│   │   ├── trace-summarizer.md
│   │   └── behavior-reviewer.md
│   └── tools/
│       └── session_eval.ts
│
├── prompts/
│   └── targets/
│       └── example/
│           ├── orchestrator.md
│           └── subagents/
│
├── sessions/
│   └── raw/
│       └── .gitkeep
│
├── runs/
│   └── .gitkeep
│
└── scripts/
    └── session_eval/
        ├── common.py
        ├── prepare.py
        ├── store_summary.py
        ├── build_map.py
        └── finalize.py
```

### `.opencode/tools/session_eval.ts`

Project-local OpenCode custom tool. Из одного файла экспортируются четыре инструмента:

- `session_eval_prepare`
- `session_eval_store_summary`
- `session_eval_build_map`
- `session_eval_finalize`

TypeScript здесь только OpenCode-адаптер. Он валидирует входную схему и запускает соответствующий Python-скрипт через stdin/stdout JSON.

### `.opencode/agents/`

Канонические project-local определения агентов OpenCode. Каждый Markdown-файл содержит YAML frontmatter с mode/permissions/model settings и собственно system prompt в теле файла:

- `session-evaluator.md` — primary workflow controller;
- `trace-summarizer.md` — hidden SLM-компрессор одного chunk;
- `behavior-reviewer.md` — hidden сильная LLM для финального анализа.

Имена файлов являются именами агентов OpenCode. Эти файлы нужно версионировать: изменение evaluator prompt или permissions должно быть видно в Git.

### `prompts/targets/`

Prompt анализируемой системы: оркестратор и relevant subagents.

Рекомендуемый layout:

```text
prompts/targets/my-agent-v1/
├── orchestrator.md
└── subagents/
    ├── coder.md
    └── researcher.md
```

Можно передать каталог промтов либо файл оркестратора. При передаче файла поиск упомянутых агентов охватывает соседние файлы и относительные пути из текста оркестратора; отсутствующие или неоднозначные соответствия требуют решения пользователя. Копии промтов в run сохраняют источник и хеш. Реальные target prompts по умолчанию исключены из Git, чтобы избежать случайной публикации содержимого анализируемой системы. Версионируйте только проверенные на чувствительные данные промты после сознательного изменения правил игнорирования.

### `sessions/raw/`

Сюда кладутся JSON export завершённых OpenCode sessions. Каталог исключён из Git, потому что export может содержать исходный код, reasoning, пользовательские данные, tool output и секреты.

Рекомендуемый layout — по одной папке на сессию:

```text
sessions/raw/002/
├── session-ses_f2cc.json
└── session-ses_f2cc.md
```

`session_eval_prepare` принимает как файл, так и папку. При передаче папки она сканируется на session export, корневая сессия выбирается автоматически; если корней несколько, инструмент сообщает неоднозначность и просит выбрать файл. Дочерние сессии обнаруживаются рекурсивно из вызовов `task`; существующие JSON берутся из той же папки, а недостающие экспортируются туда же через CLI. Если экспорт недоступен, неполнота дерева отмечается в результате.

### `runs/`

Производные данные каждого анализа. Полностью исключены из Git по умолчанию.

Каждый run имеет вид:

```text
runs/<run-id>/
├── manifest.json
├── raw/
│   ├── session-export.json
│   ├── child-sessions/
│   └── prompts/
├── normalized/
│   ├── session.json
│   └── events.json
├── chunks/
│   └── b00001-....json
├── summaries/
│   └── b00001-....json
├── derived/
│   ├── session-map.json
│   └── session-map.md
└── reports/
    ├── review.json
    └── report.md
```

Корневой export, доступные экспорты детей и prompt копируются внутрь run, поэтому конкретный анализ остаётся воспроизводимым даже если исходные файлы позже изменились.

## Агенты

Агенты определены отдельными project-local Markdown-файлами в `.opencode/agents/`. `opencode.jsonc` хранит только общую конфигурацию проекта и `default_agent`.

### `session-evaluator`

Primary agent и default agent проекта.

Ему запрещены shell, edit/write, web и произвольное чтение файлов. Разрешены только:

- `session_eval_*` tools;
- `question` для выбора, как поступить с отсутствующими экспортами и промтами;
- вызов `trace-summarizer`;
- вызов `behavior-reviewer`.

Таким образом workflow ограничен не только prompt, но и permissions OpenCode.

### `trace-summarizer`

Hidden subagent для SLM.

Имеет только read-доступ к подготовленным `runs/**/chunks/*.json`. Не имеет shell, edit, task или evaluator tools.

Он не оценивает качество поведения. Его задача — semantic compression с фиксированной JSON-схемой для:

- `user_message`;
- `reasoning`;
- `assistant_response`;
- `subtask_request`.

Маленькие semantic blocks SLM не вызывают: они переходят в карту verbatim. Tool calls/results вообще не суммаризируются моделью — они разбираются скриптами детерминированно.

### `behavior-reviewer`

Hidden subagent для сильной LLM.

Получает только:

- `derived/session-map.json`;
- копии prompt, относящихся к анализируемой системе.

Raw session намеренно не входит в его normal review boundary. Это уменьшает контекст и делает анализ ближе к идее `raw trace → semantic codec → final evaluator`.

Финальный reviewer анализирует:

- следование цели;
- `knowledge_available_but_ignored`;
- friction/repeated failures;
- recovery;
- user corrections;
- проблемы промтов оркестратора и предоставленных сабагентов;
- минимальные prompt changes;
- доказательный вердикт о необходимости дополнительного агента — `yes`, `no` или `insufficient_evidence` — с коротким объяснением на русском языке.

Человекочитаемый `report.md` использует русские заголовки, подписи и расшифровки классификаций; машинный `review.json` сохраняет исходные значения enum. Изменения промтов в отчёте сгруппированы по `name` агента, а при его отсутствии — по имени исходного файла.

Итогового числового score в MVP нет.

## Настройка моделей

В agent Markdown-файлах уже заданы модели `gate/codex-terra` (оркестратор), `gate/codex-luna-6` (компрессор) и `gate/codex-sol-6` (ревьюер). Перед запуском настройте доступ к этому provider либо замените `model` во frontmatter всех трёх файлов на доступные вам модели. Например:

```yaml
# .opencode/agents/trace-summarizer.md
model: your-provider/your-small-model

# .opencode/agents/behavior-reviewer.md
model: your-provider/your-strong-model
```

Для локальной SLM можно использовать любой provider/model, который уже настроен в OpenCode (например Ollama/LM Studio/OpenAI-compatible endpoint).

Для `.opencode/agents/session-evaluator.md` также выберите доступную модель: его собственная семантическая работа намеренно минимальна.

## Быстрый старт

Нужны OpenCode CLI в `PATH`, Python 3.10+ как `python3` и доступ к настроенным моделям. При использовании Markdown-экспорта или автоматическом экспорте дочерних сессий исходные сессии должны быть доступны в локальном storage OpenCode. Project-local tool использует `@opencode-ai/plugin`; OpenCode устанавливает эту зависимость в `.opencode/` при запуске.

### 1. Экспортировать сессию OpenCode

OpenCode CLI умеет отдавать session export как JSON:

```bash
mkdir -p sessions/raw/002
opencode export <session-id> > sessions/raw/002/session-ses_f2cc.json
```

Или положите уже существующий export в `sessions/raw/`. Markdown-экспорт (`.md`) тоже принимается: `session_eval_prepare` извлекает из его шапки `Session ID` и сам переэкспортирует сессию в JSON через `opencode export` (сессия должна существовать в storage этой машины).

CLI export не включает внутренние сообщения дочерних сессий: `opencode export` отдаёт только одну сессию, а не её потомков. Для полного дерева `session_eval_prepare` рекурсивно находит дочерние сессии из task-метаданных (`sessionId`) и автоматически экспортирует недостающие через `opencode export` в папку сессии; вопрос пользователю задаётся, если автоэкспорт не удался или источник неоднозначен. См. https://opencode.ai/docs/cli/ (`opencode export`) и https://opencode.ai/docs/sdk/ (`session.children`).

> Внимание: `session_eval_prepare` копирует исходные export и промты в run без санитизации. Если в `sessions/raw/` или `prompts/targets/` лежат чувствительные локальные файлы (исходный код, reasoning, tool output, секреты), они попадут в `runs/<run-id>/raw/` как есть. Каталоги `sessions/raw/` и `runs/` исключены из Git по умолчанию.

### 2. Положить анализируемые prompt

Например, папку агента (оркестратор + сабагенты):

```text
prompts/targets/project-v1/
├── orchestrator.md
└── subagents/
    ├── coder.md
    └── researcher.md
```

`orchestrator_prompt` принимает как файл, так и папку агента. В папке выбирается `orchestrator.md`, либо единственный top-level `.md` с `mode: primary|all`, либо единственный top-level `.md`; остальные файлы с `mode: subagent` в корне и `subagents/*.md` (кроме README) подхватываются как сабагенты.

### 3. При необходимости настроить SLM/LLM

Добавьте/измените `model` во frontmatter `.opencode/agents/trace-summarizer.md` и `.opencode/agents/behavior-reviewer.md`.

### 4. Запустить OpenCode из корня проекта

```bash
opencode
```

`session-evaluator` указан как `default_agent`.

### 5. Дать задачу оркестратору

Достаточно двух путей: папка сессии и папка агента.

Пример с папкой сессии (рекомендуется):

```text
Analyze sessions/raw/002

Orchestrator prompt:
prompts/targets/project-v1
```

Пример с файлом сессии (обратная совместимость):

```text
Analyze sessions/raw/002/session-ses_f2cc.json

Orchestrator prompt:
prompts/targets/project-v1
```

`session_path` принимает как файл, так и папку сессии; `orchestrator_prompt` — как файл, так и папку агента. Если локальное storage недоступно для Markdown-экспорта или дочерних сессий, `session_eval_prepare` сообщит об ошибке или неполноте дерева; контроллер предложит указать доступные файлы либо продолжить с ограничениями.

После завершения основной человекочитаемый результат будет в:

```text
runs/<run-id>/reports/report.md
```

А компактная карта сессии — в:

```text
runs/<run-id>/derived/session-map.json
runs/<run-id>/derived/session-map.md
```

Все входные данные (корневой export, найденные экспорты детей и промты) копируются внутрь `runs/<run-id>/raw/`, поэтому конкретный анализ остаётся воспроизводимым даже если исходные файлы позже изменились.

## Что делает deterministic layer

`prepare.py`:

- принимает файл или папку сессии; при папке сканирует session export и выбирает корневую сессию (или сообщает неоднозначность);
- проверяет JSON export;
- копирует корневой и найденные дочерние экспорты и промты в immutable-by-workflow run input;
- рекурсивно обнаруживает дочерние сессии из вызовов `task` и экспортирует отсутствующие JSON рядом с корневым экспортом;
- разбирает `messages[].info` и `messages[].parts`;
- выделяет user/assistant/reasoning semantic blocks отдельно для каждой доступной сессии, подготавливает большие блоки к сжатию;
- выделяет tool/subtask/retry/compaction и другие события;
- не отправляет tool output в SLM;
- создаёт manifest и chunks.

`store_summary.py`:

- принимает JSON SLM;
- проверяет схему в зависимости от kind;
- отклоняет неполный/невалидный output;
- сохраняет summary с source refs.

`build_map.py`:

- не запускается, пока не существуют все обязательные summaries;
- объединяет semantic blocks и deterministic events;
- считает tool status/tool counts;
- выделяет permission-like failures и повтор одинакового failed `tool + input`;
- связывает дочерние сессии и возвращённые результаты делегирования, когда доступны отдельные экспорты;
- считает события retry, вызовы инструментов и результаты восстановления;
- строит компактную timeline с идентификаторами доказательств.

`finalize.py`:

- проверяет структуру final LLM review и обязательный вердикт о дополнительном агенте;
- сохраняет JSON;
- рендерит `report.md`.

## Почему не всё отдано LLM

Объективные факты должны оставаться алгоритмическими:

- какой tool был вызван;
- с какими аргументами;
- был ли status `completed/error`;
- повторялся ли идентичный failed call;
- сколько было retry;
- какие message/part id являются источником события.

SLM нужна только как semantic codec больших текстовых блоков. Сильная LLM нужна только там, где появляется суждение: какое правило применялось, почему prompt мог не сработать и какое минимальное изменение имеет смысл.

## Текущие ограничения MVP

1. Parser рассчитан на текущую экспортную форму OpenCode `info + messages[{info, parts}]`, но intentionally tolerant к дополнительным полям. При изменении upstream schema неизвестные part types не ломают исходный export, но пока не получают специальной семантики.
2. Экспорт корневой сессии содержит вызовы `task` и ответы сабагентов, но не их внутренние сообщения или вызовы инструментов. `session_eval_prepare` рекурсивно находит дочерние сессии из вызовов `task`, использует существующие экспорты из папки сессии или экспортирует недостающие туда же через OpenCode CLI; если ребёнок недоступен, отчёт явно ограничивает выводы по нему.
3. `unused tool result` и «ветка не повлияла на решение» не определяются детерминированно в MVP: это требует semantic judgment reviewer'а.
4. Никакой automatic prompt patching нет. Reviewer только предлагает изменения; исходные prompt остаются неизменными.
5. Нет общей числовой оценки adherence. Основной выход — evidence-backed deviations и prompt findings.

## Git policy

По умолчанию Git хранит:

- evaluator code;
- evaluator agent definitions/prompts (`.opencode/agents/`);
- только явные примерные target prompts из `prompts/targets/example/`;
- configuration;
- README и MIT `LICENSE`.

Git не хранит:

- raw session exports;
- generated runs/reports/summaries;
- все target prompts, кроме явно разрешённых примерных файлов.

Перед публикацией проверяйте `git status` и состав staged-файлов: `.gitignore` не защищает файлы, уже добавленные в Git, и не мешает принудительному `git add -f`. Отчёты и карты могут содержать чувствительные данные; автоматическое редактирование секретов не гарантируется.

## OpenCode compatibility notes

Проект ориентирован на текущую документированную project-local конфигурацию OpenCode с `agent`, `permission`, `.opencode/tools/`, `default_agent` и `permission.task`.

Полезные upstream разделы:

- Custom tools: https://opencode.ai/docs/custom-tools/
- Agents: https://opencode.ai/docs/agents/
- Permissions: https://opencode.ai/docs/permissions/
- Config: https://opencode.ai/docs/config/
- CLI export: https://opencode.ai/docs/cli/

Если вы сознательно используете экспериментальную/новую V2 config syntax, поля agents/permissions отличаются — этот MVP не конвертирован под V2.

## Следующий разумный шаг после MVP

Не добавлять новые классы анализа сразу. Сначала прогнать несколько реальных больших sessions и посмотреть:

- какие chunk schemas SLM стабильно заполняет;
- какие события parser реально встречает;
- где final reviewer просит raw evidence;
- какие deviation classifications повторяются.

Только после этого имеет смысл добавлять cross-session aggregation и регрессионное сравнение Prompt v1 ↔ Prompt v2.
