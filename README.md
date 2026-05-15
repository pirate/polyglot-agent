# polyglot-agent

`polyglot-agent` is a Codex-backed harness for keeping a TypeScript codebase mapped into Python, Go, Rust, and Ruby packages.

The tool does not translate code by itself. It builds a strict repair prompt, discovers TypeScript source files, maps them to target-language files, embeds relevant `AGENTS.md` guidance, and invokes `codex exec` to repair drift.

## What It Enforces

- TypeScript is the canonical source.
- Public API shape, naming, signatures, return values, errors, defaults, async behavior, event names, and serialization semantics must match across selected languages.
- Public callable and type names preserve the TypeScript name verbatim where the target language allows it.
- Casing transforms are category-specific: fields, locals, config keys, fixture data, JSON/event payload values, and non-callable values use `snake_case`.
- Existing target-language files are authoritative when they already correspond to a TypeScript source file.
- Generated paths are candidate paths for missing files only; the agent must first prove that the target runtime owns the behavior.
- Runtime ownership beats file symmetry. Frontend/browser, extension/service-worker, server-only, CLI/build, codegen, native platform, and other source-runtime-only TypeScript files may be intentionally unmapped in target languages that do not own that runtime.
- Tests and examples are part of the mapping contract when they exercise target-owned behavior, not optional follow-up work.
- Scoped runs stay scoped: unrelated validation failures should be reported, not repaired, unless they were caused by the current edits.

## Install

From a checkout:

```bash
python3 -m pip install -e .
```

Or run the local wrapper directly:

```bash
bin/polyglot-map --help
```

The harness expects the Codex CLI to be available as `codex` unless you pass `--codex /path/to/codex`.

## One-Shot Drift Repair

Run against the current repository:

```bash
polyglot-map run \
  --source-root ./client/js \
  --target python=./client/python \
  --target go=./client/go \
  --package-name my_package \
  --languages ts,python,golang
```

Inspect the exact prompt without changing files:

```bash
polyglot-map run \
  --dry-run \
  --source-root ./client/js \
  --target python=./client/python \
  --target go=./client/go \
  --package-name my_package \
  --languages ts,python,golang
```

## Watch Mode

Watch TypeScript files and repair drift after changes:

```bash
polyglot-map watch \
  --source-root ./client/js \
  --target python=./client/python \
  --target go=./client/go \
  --languages ts,python,golang \
  --interval 2
```

Add `--initial` to run a full repair once before watching.

## Mapping Preview

Print the source-to-target manifest:

```bash
polyglot-map map \
  --source-root ./client/js \
  --target python=./client/python \
  --target go=./client/go \
  --package-name my_package \
  --languages ts,python,golang
```

Each target path is marked with one of:

- `mapped-existing`: the generated mapped path already exists.
- `existing-equivalent`: a target-language file with an equivalent source stem already exists, so the agent must update that file in place.
- `generated-missing`: no equivalent file exists yet, so the generated path is a candidate creation target only if the target runtime owns that behavior.

Each manifest entry also includes a `mapping_contract` and allowed mapping decisions. The invoked agent must classify the TypeScript source responsibility before editing:

- `implement-in-target`: target runtime owns this behavior and needs a real implementation or update.
- `existing-target-equivalent`: an established target file owns the behavior.
- `source-runtime-only`: TypeScript owns behavior that runs in a runtime unavailable to the target language.
- `generated-artifact`: parity belongs to generator/schema output or generated metadata.
- `test-contract-only`: parity should be asserted through existing public behavior tests.
- `unsupported-runtime-limitation`: the target runtime cannot express the behavior; report the reason without stubs.

## Defaults

When no options are provided:

- `--repo` defaults to the current working directory.
- `--source-root` defaults to the repository root.
- Target roots default to `python/`, `go/`, `rust/`, and `ruby/` under the repository root.
- `--package-name` defaults to the repository directory name converted to `snake_case`.
- Guidance files default to `AGENTS.md` and direct child `*/AGENTS.md` files under the repository root.

Override guidance discovery with one or more `--agents-glob` values:

```bash
polyglot-map run \
  --agents-glob AGENTS.md \
  --agents-glob 'packages/*/AGENTS.md'
```

## Language Selection

Use `--languages` to limit a run:

```bash
polyglot-map run --languages ts,python,golang
```

Aliases include `ts`, `typescript`, `py`, `python`, `go`, `golang`, `rs`, `rust`, `rb`, and `ruby`.

## Prompt Template

The full agent prompt lives in `src/polyglot_map/system_prompt.md`.

The CLI hydrates that template with invocation-specific values such as configured languages, repository roots, discovered `AGENTS.md` content, and the JSON source manifest. Edit the Markdown file to change policy; keep Python changes focused on discovery and rendering.

## Safety Model

The generated system prompt forbids destructive git commands, silent test downgrades, TODO placeholders for mapped files, compatibility shims that hide drift, and rename-only layout churn.

For publishing or public use, prefer explicit `--source-root`, `--target`, `--package-name`, and `--agents-glob` arguments so generated prompts contain only paths and guidance you intend to share with the invoked agent.
