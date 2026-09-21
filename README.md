# OpenCode Session Evaluator — MVP

Локальный проект для анализа больших экспортированных агентских сессий OpenCode и отладки prompt-поведения оркестратора/сабагентов.

Цель — не выставить агенту абстрактную оценку, а построить компактную карту выполнения и ответить на практические вопросы:

1. где фактическая траектория выполнения разошлась с prompt-контрактом;
2. было ли правило/ограничение уже известно модели до ошибочного действия;
3. где возникала лишняя работа и повторные неуспешные действия;
4. как агент восстанавливался после ошибки;
5. где пользователь выступал внешним корректирующим контуром;
6. какие минимальные изменения prompt могут снизить вероятность повторения проблемы.

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

Эти prompt также желательно версионировать: в перспективе это позволяет сравнивать поведение Prompt v1 → Prompt v2 на одинаковых/похожих сценариях.

### `sessions/raw/`

Сюда кладутся JSON export завершённых OpenCode sessions. Каталог исключён из Git, потому что export может содержать исходный код, reasoning, пользовательские данные, tool output и секреты.

### `runs/`

Производные данные каждого анализа. Полностью исключены из Git по умолчанию.

Каждый run имеет вид:

```text
runs/<run-id>/
├── manifest.json
├── raw/
│   ├── session-export.json
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

Исходный export и prompt копируются внутрь run, поэтому конкретный анализ остаётся воспроизводимым даже если исходные prompt позже изменились.

## Агенты

Агенты определены отдельными project-local Markdown-файлами в `.opencode/agents/`. `opencode.jsonc` хранит только общую конфигурацию проекта и `default_agent`.

### `session-evaluator`

Primary agent и default agent проекта.

Ему запрещены shell, edit/write, web и произвольное чтение файлов. Разрешены только:

- `session_eval_*` tools;
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
- проблемы самого prompt;
- минимальные prompt changes.

Итогового числового score в MVP нет.

## Настройка моделей

В agent Markdown-файлах поле `model` намеренно не задано, поэтому по умолчанию используется модель, настроенная в OpenCode.

Для реального режима рекомендуется явно развести модели, добавив поле `model` во frontmatter соответствующего файла:

```yaml
# .opencode/agents/trace-summarizer.md
model: your-provider/your-small-model

# .opencode/agents/behavior-reviewer.md
model: your-provider/your-strong-model
```

Для локальной SLM можно использовать любой provider/model, который уже настроен в OpenCode (например Ollama/LM Studio/OpenAI-compatible endpoint).

Оркестратору можно оставить обычную модель: его собственная семантическая работа намеренно минимальна.

## Быстрый старт

### 1. Экспортировать сессию OpenCode

OpenCode CLI умеет отдавать session export как JSON:

```bash
opencode export <session-id> > sessions/raw/session.json
```

Или положите уже существующий export в `sessions/raw/`.

### 2. Положить анализируемые prompt

Например:

```text
prompts/targets/project-v1/orchestrator.md
prompts/targets/project-v1/subagents/coder.md
prompts/targets/project-v1/subagents/researcher.md
```

### 3. При необходимости настроить SLM/LLM

Добавьте/измените `model` во frontmatter `.opencode/agents/trace-summarizer.md` и `.opencode/agents/behavior-reviewer.md`.

### 4. Запустить OpenCode из корня проекта

```bash
opencode
```

`session-evaluator` указан как `default_agent`.

### 5. Дать задачу оркестратору

Пример:

```text
Analyze sessions/raw/session.json

Orchestrator prompt:
prompts/targets/project-v1/orchestrator.md

Relevant subagent prompts:
prompts/targets/project-v1/subagents/coder.md
prompts/targets/project-v1/subagents/researcher.md
```

После завершения основной человекочитаемый результат будет в:

```text
runs/<run-id>/reports/report.md
```

А компактная карта сессии — в:

```text
runs/<run-id>/derived/session-map.json
runs/<run-id>/derived/session-map.md
```

## Что делает deterministic layer

`prepare.py`:

- проверяет JSON export;
- копирует session и prompt в immutable-by-workflow run input;
- разбирает `messages[].info` и `messages[].parts`;
- выделяет user/assistant/reasoning semantic blocks;
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
- выделяет permission-like failures;
- выделяет повтор одинакового failed `tool + input`;
- считает retry/subtask events;
- строит компактную timeline.

`finalize.py`:

- проверяет базовую структуру final LLM review;
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
2. Вложенные/child sessions не склеиваются автоматически по отдельным export-файлам. Если subagent activity представлена внутри родительского export как `subtask`/`task`, она попадёт в карту. Полноценное соединение нескольких child exports — следующий этап, только если это реально потребуется.
3. `unused tool result` и «ветка не повлияла на решение» не определяются детерминированно в MVP: это требует semantic judgment reviewer'а.
4. Никакой automatic prompt patching нет. Reviewer только предлагает изменения; исходные prompt остаются неизменными.
5. Нет общей числовой оценки adherence. Основной выход — evidence-backed deviations и prompt findings.

## Git policy

По умолчанию Git хранит:

- evaluator code;
- evaluator agent definitions/prompts (`.opencode/agents/`);
- target prompts;
- configuration;
- README.

Git не хранит:

- raw session exports;
- generated runs/reports/summaries.

Если конкретные target prompts тоже чувствительные, добавьте `prompts/targets/` в локальный `.gitignore`.

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
