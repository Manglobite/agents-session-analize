import { tool } from "@opencode-ai/plugin"

type ToolContext = {
  worktree: string
}

type PrepareArgs = {
  session_path: string
  orchestrator_prompt?: string
  subagent_prompts?: string[]
  child_export_paths?: string[]
  chunk_chars?: number
  run_name?: string
}

type StoreSummaryArgs = {
  run_id: string
  chunk_id: string
  summary_json: string
}

type BuildMapArgs = {
  run_id: string
}

type FinalizeArgs = {
  run_id: string
  review_json: string
}

type BunProcess = {
  stdin: {
    write(data: string): unknown
    end(): unknown
  }
  stdout: BodyInit
  stderr: BodyInit
  exited: Promise<number>
  exitCode: number | null
  kill(signal?: number | string): unknown
}

type BunRuntime = {
  spawn(
    command: string[],
    options: {
      cwd: string
      stdin: "pipe"
      stdout: "pipe"
      stderr: "pipe"
    },
  ): BunProcess
}

function formatError(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}

function projectPath(worktree: string, ...parts: string[]): string {
  const root = worktree.replace(/[\\/]+$/, "")
  const suffix = parts.map((part) => part.replace(/^[\\/]+|[\\/]+$/g, "")).join("/")
  return `${root}/${suffix}`
}

function getBun(): BunRuntime {
  const runtime = (globalThis as typeof globalThis & { Bun?: BunRuntime }).Bun
  if (!runtime) {
    throw new Error("session_eval tools require the Bun runtime used by OpenCode")
  }
  return runtime
}

const RUN_PYTHON_TIMEOUT_MS = 120_000
const RUN_PREPARE_TIMEOUT_MS = 1_200_000

async function runPython(
  context: ToolContext,
  scriptName: string,
  input: Record<string, unknown>,
  timeoutMs: number,
): Promise<string> {
  const scriptPath = projectPath(context.worktree, "scripts", "session_eval", scriptName)
  const proc = getBun().spawn(["python3", scriptPath], {
    cwd: context.worktree,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
  })

  proc.stdin.write(JSON.stringify({ project_root: context.worktree, ...input }))
  proc.stdin.end()

  let timedOut = false
  const timer = setTimeout(() => {
    timedOut = true
    proc.kill()
  }, timeoutMs)

  try {
    const [output, stderr] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ])
    await proc.exited
    if (timedOut) {
      throw new Error(`${scriptName} timed out after ${timeoutMs} ms`)
    }
    if (proc.exitCode === 0) return output.trimEnd()
    if (output.trim()) {
      try {
        const result = JSON.parse(output)
        if (result && typeof result === "object" && result.ok === false) return output.trimEnd()
      } catch {}
    }
    throw new Error(stderr.trim() || output.trim() || `${scriptName} exited with code ${proc.exitCode}`)
  } finally {
    clearTimeout(timer)
  }
}

export const prepare = tool({
  description:
    "Prepare an OpenCode session for evaluation. Accepts a session export file or a session folder; the Python core scans a folder for session exports, auto-selects the root session, and returns structured ambiguity or error for the controller to ask the user. Deterministically copies inputs into a run, normalizes messages/parts, extracts tool events, and creates semantic chunks. Child session exports are discovered recursively from task metadata (sessionId) and auto-exported via the opencode CLI into the same session folder, so the reviewer sees the full session tree. Agent definition files referenced by the orchestrator prompt are searched alongside it or relative to the project root. Unmatched agent prompts and missing child exports are reported for the controller to ask the user. See https://opencode.ai/docs/cli/ for `opencode export` and https://opencode.ai/docs/sdk/ for `session.children`; the CLI export does not bundle descendant sessions. Use this first.",
  args: {
    session_path: tool.schema
      .string()
      .describe(
        "Project-relative path to an OpenCode session export file (JSON or Markdown) or a session folder, normally sessions/raw/<file>.json or sessions/raw/<folder>. A folder is scanned for session exports and the root session is auto-selected; if multiple roots are found the tool reports ambiguity. A Markdown export (.md) is auto re-exported as JSON via the opencode CLI using the session id found in its header",
      ),
    orchestrator_prompt: tool.schema
      .string()
      .optional()
      .describe(
        "Optional project-relative path to the orchestrator prompt file, or a target folder: orchestrator.md (or a single top-level .md) plus subagents/*.md are picked up automatically",
      ),
    subagent_prompts: tool.schema
      .array(tool.schema.string())
      .optional()
      .default([])
      .describe("Optional project-relative paths to relevant subagent prompt files; a folder expands to its .md files"),
    child_export_paths: tool.schema
      .array(tool.schema.string())
      .optional()
      .default([])
      .describe(
        "Optional explicit project-relative paths to child session exports. Child sessions are also discovered automatically from task tool metadata (sessionId) and auto-exported via the opencode CLI into the same session folder",
      ),
    chunk_chars: tool.schema
      .number()
      .optional()
      .default(5000)
      .describe("Semantic blocks larger than this many characters are delegated to the SLM summarizer"),
    run_name: tool.schema
      .string()
      .optional()
      .describe("Optional short label used in the run directory name"),
  },
  async execute(args: PrepareArgs, context: ToolContext) {
    try {
      return await runPython(context, "prepare.py", args, RUN_PREPARE_TIMEOUT_MS)
    } catch (error) {
      return JSON.stringify({ ok: false, error: formatError(error) })
    }
  },
})

export const store_summary = tool({
  description:
    "Validate and persist one JSON summary returned by trace-summarizer for a prepared chunk. Call once per chunk marked needs_summary=true.",
  args: {
    run_id: tool.schema.string().describe("Run identifier returned by session_eval_prepare"),
    chunk_id: tool.schema.string().describe("Chunk identifier returned by session_eval_prepare"),
    summary_json: tool.schema.string().describe("Raw JSON object emitted by trace-summarizer, with no Markdown fences"),
  },
  async execute(args: StoreSummaryArgs, context: ToolContext) {
    try {
      return await runPython(context, "store_summary.py", args, RUN_PYTHON_TIMEOUT_MS)
    } catch (error) {
      return JSON.stringify({ ok: false, error: formatError(error) })
    }
  },
})

export const build_map = tool({
  description:
    "Build the compact session map after all required SLM summaries have been stored. Computes deterministic counts/findings and writes JSON plus human-readable Markdown. The map covers the full session tree, including every discovered child session.",
  args: {
    run_id: tool.schema.string().describe("Run identifier returned by session_eval_prepare"),
  },
  async execute(args: BuildMapArgs, context: ToolContext) {
    try {
      return await runPython(context, "build_map.py", args, RUN_PYTHON_TIMEOUT_MS)
    } catch (error) {
      return JSON.stringify({ ok: false, error: formatError(error) })
    }
  },
})

export const finalize = tool({
  description:
    "Validate and persist the final JSON review produced by behavior-reviewer, then render a human-readable Markdown report.",
  args: {
    run_id: tool.schema.string().describe("Run identifier returned by session_eval_prepare"),
    review_json: tool.schema.string().describe("Raw JSON object emitted by behavior-reviewer, with no Markdown fences"),
  },
  async execute(args: FinalizeArgs, context: ToolContext) {
    try {
      return await runPython(context, "finalize.py", args, RUN_PYTHON_TIMEOUT_MS)
    } catch (error) {
      return JSON.stringify({ ok: false, error: formatError(error) })
    }
  },
})
