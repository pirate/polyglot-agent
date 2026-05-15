from __future__ import annotations

import argparse
import dataclasses
import fnmatch
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Iterable


DEFAULT_REPO = Path.cwd()
DEFAULT_AGENT_PATTERNS = ("AGENTS.md", "*/AGENTS.md")
SOURCE_LANGUAGES = ("typescript",)
TARGET_LANGUAGES = ("python", "go", "rust", "ruby")
TARGET_EXTENSIONS = {
    "python": (".py",),
    "go": (".go",),
    "rust": (".rs",),
    "ruby": (".rb",),
}
LANGUAGE_ALIASES = {
    "py": "python",
    "python": "python",
    "go": "go",
    "golang": "go",
    "rs": "rust",
    "rust": "rust",
    "rb": "ruby",
    "ruby": "ruby",
    "ts": "typescript",
    "tsx": "typescript",
    "typescript": "typescript",
}
SOURCE_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts")
IGNORED_DIRS = {
    ".git",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
}
IGNORED_GLOBS = ("*.d.ts", "*.map", "*.min.ts")
INITIALISM_NORMALIZATIONS = {
    "HTTP": "Http",
    "JSONL": "Jsonl",
    "NATS": "Nats",
    "OTel": "Otel",
    "SQLite": "Sqlite",
    "SQL": "Sql",
}
PROMPT_TEMPLATE_NAME = "system_prompt.md"
MAPPING_DECISIONS = (
    "implement-in-target",
    "existing-target-equivalent",
    "source-runtime-only",
    "generated-artifact",
    "test-contract-only",
    "unsupported-runtime-limitation",
)


@dataclasses.dataclass(frozen=True)
class Target:
    language: str
    root: Path
    package_name: str = "package"


@dataclasses.dataclass(frozen=True)
class HarnessConfig:
    repo: Path
    source_root: Path
    targets: tuple[Target, ...]
    agent_patterns: tuple[str, ...]


def default_package_name(repo: Path) -> str:
    return snake_case(repo.name.replace("-", "_")) or "package"


def normalize_language(language: str) -> str:
    normalized = language.strip().lower().replace("_", "-")
    if normalized not in LANGUAGE_ALIASES:
        expected = ", ".join(sorted(LANGUAGE_ALIASES))
        raise argparse.ArgumentTypeError(f"unsupported language {language!r}; expected one of {expected}")
    return LANGUAGE_ALIASES[normalized]


def parse_languages(values: Iterable[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    languages: list[str] = []
    for value in values:
        for language in value.split(","):
            if language.strip():
                languages.append(normalize_language(language))
    return tuple(dict.fromkeys(languages))


def snake_case(name: str) -> str:
    name = name.strip()
    for initialism, normalized in INITIALISM_NORMALIZATIONS.items():
        name = name.replace(initialism, normalized)
    name = re.sub(r"[\s\-]+", "_", name)
    name = name.replace(".", "_")
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_").lower() or "index"


def remove_ts_suffix(path: PurePosixPath) -> str:
    name = path.name
    for suffix in SOURCE_EXTENSIONS:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def is_source_path(path: Path) -> bool:
    if path.suffix not in SOURCE_EXTENSIONS:
        return False
    name = path.name
    if any(fnmatch.fnmatch(name, pattern) for pattern in IGNORED_GLOBS):
        return False
    return not any(part in IGNORED_DIRS for part in path.parts)


def is_source_file(path: Path) -> bool:
    return path.is_file() and is_source_path(path)


def iter_source_files(source_root: Path) -> list[Path]:
    if not source_root.exists():
        return []
    found: list[Path] = []
    for root, dirs, files in os.walk(source_root):
        dirs[:] = [dirname for dirname in dirs if dirname not in IGNORED_DIRS]
        root_path = Path(root)
        for filename in files:
            path = root_path / filename
            if is_source_file(path):
                found.append(path)
    return sorted(found)


def normalize_source_rel(source_root: Path, path: Path) -> PurePosixPath:
    try:
        return PurePosixPath(path.resolve().relative_to(source_root.resolve()).as_posix())
    except ValueError:
        return PurePosixPath(path.as_posix())


def is_test_rel(rel: PurePosixPath) -> bool:
    stem = remove_ts_suffix(rel)
    return "tests" in rel.parts or stem.startswith("test.") or stem.endswith(".test") or stem.endswith("_test")


def source_kind(rel: PurePosixPath) -> str:
    if rel.parts and rel.parts[0] == "examples":
        return "example"
    if is_test_rel(rel):
        return "test"
    return "implementation"


def canonical_test_name(rel: PurePosixPath, *, preserve_test_dot_name: bool = False) -> str:
    stem = remove_ts_suffix(rel)
    if stem.startswith("test."):
        name = stem.removeprefix("test.")
        return name if preserve_test_dot_name else f"test_{snake_case(name)}"
    if stem.endswith(".test"):
        return f"test_{snake_case(stem.removesuffix('.test'))}"
    if stem.endswith("_test"):
        return f"test_{snake_case(stem.removesuffix('_test'))}"
    return f"test_{snake_case(stem)}"


def path_without_source_prefix(rel: PurePosixPath) -> tuple[str, ...]:
    parts = tuple(rel.parts)
    if parts and parts[0] in {"src", "tests", "examples"}:
        return parts[1:]
    return parts


def implementation_parts(rel: PurePosixPath) -> tuple[str, ...]:
    parts = path_without_source_prefix(rel)
    return parts[:-1]


def embedded_test_dirs(rel: PurePosixPath) -> tuple[str, ...]:
    parts = path_without_source_prefix(rel)
    if "tests" not in parts:
        return parts[:-1]
    test_index = parts.index("tests")
    return parts[:test_index] + parts[test_index + 1 : -1]


def map_target_path(rel: PurePosixPath, target: Target) -> Path:
    kind = source_kind(rel)
    stem = remove_ts_suffix(rel)
    root = target.root
    implementation_dir = Path(*map(snake_case, implementation_parts(rel)))
    test_dir = Path(*map(snake_case, embedded_test_dirs(rel)))

    if target.language == "python":
        if kind == "implementation":
            filename = "__init__.py" if stem == "index" else f"{snake_case(stem)}.py"
            return root / target.package_name / implementation_dir / filename
        if kind == "example":
            return root / "examples" / implementation_dir / f"{snake_case(stem)}.py"
        return root / "tests" / test_dir / f"{canonical_test_name(rel)}.py"

    if target.language == "go":
        if kind == "implementation":
            return root / implementation_dir / f"{snake_case(stem)}.go"
        if kind == "example":
            return root / "examples" / implementation_dir / f"{snake_case(stem)}.go"
        if remove_ts_suffix(rel).startswith("test."):
            filename = f"{canonical_test_name(rel, preserve_test_dot_name=True)}_test.go"
            return root / test_dir / filename
        filename = f"{snake_case(canonical_test_name(rel).removeprefix('test_'))}_test.go"
        return root / "tests" / test_dir / filename

    if target.language == "rust":
        if kind == "implementation":
            filename = "lib.rs" if stem == "index" else f"{snake_case(stem)}.rs"
            return root / "src" / implementation_dir / filename
        if kind == "example":
            return root / "examples" / implementation_dir / f"{snake_case(stem)}.rs"
        return root / "tests" / test_dir / f"{canonical_test_name(rel)}.rs"

    if target.language == "ruby":
        if kind == "implementation":
            filename = f"{target.package_name}.rb" if stem == "index" else f"{snake_case(stem)}.rb"
            return root / "lib" / target.package_name / implementation_dir / filename
        if kind == "example":
            return root / "examples" / implementation_dir / f"{snake_case(stem)}.rb"
        return root / "test" / test_dir / f"{canonical_test_name(rel)}.rb"

    raise ValueError(f"unsupported target language: {target.language}")


def target_stem_keys(rel: PurePosixPath) -> set[str]:
    stem = remove_ts_suffix(rel)
    if stem.startswith("test."):
        base = stem.removeprefix("test.")
    elif stem.endswith(".test"):
        base = stem.removesuffix(".test")
    elif stem.endswith("_test"):
        base = stem.removesuffix("_test")
    else:
        base = stem

    base_key = snake_case(base)
    stem_key = snake_case(stem)
    keys = {base_key, stem_key, f"test_{base_key}", f"{base_key}_test"}
    if stem == "index":
        keys.add("__init__")
    return keys


def candidate_target_key(path: Path) -> str:
    if path.name == "main.go":
        return snake_case(path.parent.name)
    return snake_case(path.stem)


def target_candidate_score(path: Path, default_path: Path) -> tuple[int, int, str]:
    if path == default_path:
        return (0, len(path.parts), path.as_posix())
    if path.name == default_path.name:
        return (10, len(path.parts), path.as_posix())
    if path.parent == default_path.parent:
        return (20, len(path.parts), path.as_posix())
    return (30, len(path.parts), path.as_posix())


def resolve_target_path(rel: PurePosixPath, target: Target) -> tuple[Path, str, Path | None]:
    default_path = map_target_path(rel, target)
    if default_path.exists():
        return default_path, "mapped-existing", None

    root = target.root
    suffixes = TARGET_EXTENSIONS[target.language]
    keys = target_stem_keys(rel)
    if root.exists():
        candidates = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in suffixes
            and not any(part in IGNORED_DIRS for part in path.parts)
            and candidate_target_key(path) in keys
        ]
        if candidates:
            return sorted(candidates, key=lambda path: target_candidate_score(path, default_path))[0], "existing-equivalent", default_path

    return default_path, "generated-missing", None


def default_config(repo: Path = DEFAULT_REPO, agent_patterns: tuple[str, ...] = DEFAULT_AGENT_PATTERNS) -> HarnessConfig:
    repo = repo.expanduser().resolve()
    package_name = default_package_name(repo)
    return HarnessConfig(
        repo=repo,
        source_root=repo,
        targets=(
            Target("python", repo / "python", package_name),
            Target("go", repo / "go", package_name),
            Target("rust", repo / "rust", package_name),
            Target("ruby", repo / "ruby", package_name),
        ),
        agent_patterns=agent_patterns,
    )


def parse_target(value: str) -> Target:
    if "=" not in value:
        raise argparse.ArgumentTypeError("targets must be formatted as language=/absolute/or/relative/path")
    language, raw_path = value.split("=", 1)
    language = normalize_language(language)
    if language in SOURCE_LANGUAGES:
        raise argparse.ArgumentTypeError("TypeScript is the source language and cannot be used as a target")
    return Target(language, Path(raw_path).expanduser().resolve())


def config_from_args(args: argparse.Namespace) -> HarnessConfig:
    agent_patterns = tuple(args.agents_glob) if args.agents_glob else DEFAULT_AGENT_PATTERNS
    config = default_config(Path(args.repo), agent_patterns)
    source_root = Path(args.source_root).expanduser().resolve() if args.source_root else config.source_root
    package_name = args.package_name or default_package_name(config.repo)
    if args.target:
        targets = tuple(dataclasses.replace(target, package_name=package_name) for target in args.target)
    else:
        targets = tuple(dataclasses.replace(target, package_name=package_name) for target in config.targets)
    selected_languages = parse_languages(args.languages or args.only)
    if selected_languages:
        selected_targets = {language for language in selected_languages if language in TARGET_LANGUAGES}
        targets = tuple(target for target in targets if target.language in selected_targets)
    return HarnessConfig(repo=config.repo, source_root=source_root, targets=targets, agent_patterns=config.agent_patterns)


def read_agents(repo: Path, patterns: Iterable[str]) -> tuple[list[Path], list[str], str]:
    paths: list[Path] = []
    unmatched: list[str] = []
    for raw_pattern in patterns:
        pattern_path = Path(raw_pattern).expanduser()
        pattern = str(pattern_path if pattern_path.is_absolute() else repo / pattern_path)
        matches = [
            match
            for match in sorted(glob.glob(pattern, recursive=True))
            if Path(match).is_file() and not any(part in IGNORED_DIRS for part in Path(match).parts)
        ]
        if matches:
            paths.extend(Path(path) for path in matches)
        else:
            unmatched.append(raw_pattern)
    paths = sorted({path.resolve(): path.resolve() for path in paths}.values())
    chunks: list[str] = []
    for path in paths:
        try:
            text = path.read_text()
        except OSError as err:
            text = f"[unreadable: {err}]"
        chunks.append(f"--- {path} ---\n{text.rstrip()}\n")
    return paths, unmatched, "\n".join(chunks).rstrip()


def changed_source_files(config: HarnessConfig, raw_paths: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for raw_path in raw_paths:
        raw = Path(raw_path).expanduser()
        raw_candidates = [raw] if raw.is_absolute() else [Path.cwd() / raw, config.repo / raw, config.source_root / raw]
        candidates = [candidate.resolve() for candidate in raw_candidates]
        for path in candidates:
            if path.exists() and is_source_path(path):
                paths.append(path)
                break
        else:
            for path in candidates:
                if is_source_path(path):
                    paths.append(path)
                    break
    unique = {path.resolve(): path.resolve() for path in paths}
    return sorted(unique.values())


def build_manifest(config: HarnessConfig, source_files: list[Path]) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for path in source_files:
        rel = normalize_source_rel(config.source_root, path)
        target_paths: dict[str, str] = {}
        default_target_paths: dict[str, str] = {}
        target_path_sources: dict[str, str] = {}
        for target in config.targets:
            target_path, source, default_path = resolve_target_path(rel, target)
            target_paths[target.language] = str(target_path)
            target_path_sources[target.language] = source
            if default_path is not None:
                default_target_paths[target.language] = str(default_path)
        manifest.append(
            {
                "source": str(path),
                "source_rel": rel.as_posix(),
                "source_exists": path.exists(),
                "kind": source_kind(rel),
                "mapping_contract": "candidate-targets-require-runtime-ownership-decision",
                "allowed_mapping_decisions": list(MAPPING_DECISIONS),
                "targets": target_paths,
                "target_path_sources": target_path_sources,
                "generated_default_targets": default_target_paths,
            }
        )
    return manifest


def load_prompt_template() -> str:
    return resources.files(__package__).joinpath(PROMPT_TEMPLATE_NAME).read_text()


def render_prompt_template(template: str, values: dict[str, str]) -> str:
    return template.format(**values)


def build_prompt(config: HarnessConfig, source_files: list[Path], *, mode: str) -> str:
    agent_paths, unmatched_agent_patterns, agent_text = read_agents(config.repo, config.agent_patterns)
    manifest = build_manifest(config, source_files)
    selected_languages = ", ".join(("typescript", *(target.language for target in config.targets)))
    target_summary = "\n".join(f"- {target.language}: {target.root}" for target in config.targets)
    agent_summary = "\n".join(f"- {path}" for path in agent_paths) if agent_paths else "- no AGENTS.md files matched"
    unmatched_agent_summary = "\n".join(f"- {pattern}" for pattern in unmatched_agent_patterns)
    manifest_text = json.dumps(manifest, indent=2)
    values = {
        "mode": mode,
        "selected_languages": selected_languages,
        "repo": str(config.repo),
        "source_root": str(config.source_root),
        "target_summary": target_summary or "- none configured; this invocation can only inspect TypeScript source",
        "agent_patterns": "\n".join(f"- {pattern}" for pattern in config.agent_patterns),
        "agent_summary": agent_summary,
        "unmatched_agent_summary": unmatched_agent_summary or "- all patterns matched",
        "agent_text": agent_text or "[No AGENTS.md content was found for this invocation.]",
        "manifest_text": manifest_text,
    }
    return render_prompt_template(load_prompt_template(), values)


def run_codex(config: HarnessConfig, prompt: str, args: argparse.Namespace) -> int:
    codex = args.codex or shutil.which("codex")
    if not codex:
        print("codex executable not found. Install Codex CLI or pass --codex /path/to/codex.", file=sys.stderr)
        return 127

    command = [
        codex,
        "-s",
        args.sandbox,
        "-a",
        "never",
        "exec",
        "-C",
        str(config.repo),
        "--skip-git-repo-check",
    ]
    if args.model:
        command.extend(["-m", args.model])
    command.append("-")

    print(f"running: {' '.join(command[:-1])} -", file=sys.stderr)
    completed = subprocess.run(command, input=prompt, text=True, check=False)
    return completed.returncode


def snapshot(source_root: Path) -> dict[Path, tuple[int, int]]:
    files = iter_source_files(source_root)
    state: dict[Path, tuple[int, int]] = {}
    for path in files:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        state[path] = (stat.st_mtime_ns, stat.st_size)
    return state


def changed_since(previous: dict[Path, tuple[int, int]], current: dict[Path, tuple[int, int]]) -> list[Path]:
    changed: list[Path] = []
    all_paths = set(previous) | set(current)
    for path in sorted(all_paths):
        if previous.get(path) != current.get(path) and is_source_path(path):
            changed.append(path)
    return changed


def command_run(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    if not config.targets:
        print("no target languages selected; include at least one of python, go/golang, rust, ruby", file=sys.stderr)
        return 2
    if args.changed:
        source_files = changed_source_files(config, args.changed)
    else:
        source_files = iter_source_files(config.source_root)

    if not source_files:
        print(f"no TypeScript source files found under {config.source_root}", file=sys.stderr)
        return 2

    prompt = build_prompt(config, source_files, mode="one-shot drift repair")
    if args.dry_run:
        print(prompt)
        return 0
    return run_codex(config, prompt, args)


def command_watch(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    if not config.targets:
        print("no target languages selected; include at least one of python, go/golang, rust, ruby", file=sys.stderr)
        return 2
    previous = snapshot(config.source_root)
    print(f"watching {config.source_root} for TypeScript changes")

    if args.initial:
        prompt = build_prompt(config, sorted(previous), mode="watch initial drift repair")
        if args.dry_run:
            print(prompt)
        else:
            exit_code = run_codex(config, prompt, args)
            if exit_code != 0:
                return exit_code

    try:
        while True:
            time.sleep(args.interval)
            current = snapshot(config.source_root)
            changed = changed_since(previous, current)
            previous = current
            if not changed:
                continue

            time.sleep(args.debounce)
            current = snapshot(config.source_root)
            changed = changed_since(previous, current) or changed
            previous = current

            print(f"detected {len(changed)} changed TypeScript file(s)")
            prompt = build_prompt(config, changed, mode="watch incremental drift repair")
            if args.dry_run:
                print(prompt)
                continue
            exit_code = run_codex(config, prompt, args)
            if exit_code != 0 and args.stop_on_error:
                return exit_code
    except KeyboardInterrupt:
        print("watch stopped")
        return 0


def command_map(args: argparse.Namespace) -> int:
    config = config_from_args(args)
    source_files = iter_source_files(config.source_root)
    manifest = build_manifest(config, source_files)
    if args.json:
        print(json.dumps(manifest, indent=2))
        return 0

    for entry in manifest:
        print(f"{entry['source_rel']} ({entry['kind']})")
        print(f"  mapping  {entry['mapping_contract']}")
        targets = entry["targets"]
        target_path_sources = entry["target_path_sources"]
        assert isinstance(targets, dict)
        assert isinstance(target_path_sources, dict)
        for language, path in targets.items():
            status = "exists" if Path(path).exists() else "missing"
            source = target_path_sources.get(language, "unknown")
            print(f"  {language:6} {path} [{status}, {source}]")
    return 0


def add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", default=str(DEFAULT_REPO), help="repository root passed to codex exec")
    parser.add_argument("--source-root", help="TypeScript source package root; defaults to REPO")
    parser.add_argument("--target", action="append", type=parse_target, help="target mapping: language=/path/to/root")
    parser.add_argument("--package-name", help="target package/module name for Python and Ruby paths")
    parser.add_argument(
        "--languages",
        action="append",
        help="comma-separated language set, e.g. ts,python,golang; TypeScript is always the source",
    )
    parser.add_argument("--only", action="append", help="deprecated alias for --languages")
    parser.add_argument(
        "--agents-glob",
        action="append",
        help="AGENTS.md glob/path embedded in the prompt; repeat to override defaults",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="polyglot-map")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run one-shot drift repair")
    add_common_options(run_parser)
    run_parser.add_argument("--changed", action="append", help="changed TypeScript source path to scope the run")
    run_parser.add_argument("--dry-run", action="store_true", help="print prompt without invoking Codex")
    run_parser.add_argument("--codex", help="path to Codex CLI executable")
    run_parser.add_argument("--model", help="Codex model override")
    run_parser.add_argument(
        "--sandbox",
        default="danger-full-access",
        choices=("read-only", "workspace-write", "danger-full-access"),
    )
    run_parser.set_defaults(func=command_run)

    watch_parser = subparsers.add_parser("watch", help="watch TypeScript files and repair drift after changes")
    add_common_options(watch_parser)
    watch_parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    watch_parser.add_argument("--debounce", type=float, default=0.75, help="settle time after a detected change")
    watch_parser.add_argument("--initial", action="store_true", help="run a full repair once before watching")
    watch_parser.add_argument("--stop-on-error", action="store_true", help="stop watch mode if Codex exits non-zero")
    watch_parser.add_argument("--dry-run", action="store_true", help="print prompts without invoking Codex")
    watch_parser.add_argument("--codex", help="path to Codex CLI executable")
    watch_parser.add_argument("--model", help="Codex model override")
    watch_parser.add_argument(
        "--sandbox",
        default="danger-full-access",
        choices=("read-only", "workspace-write", "danger-full-access"),
    )
    watch_parser.set_defaults(func=command_watch)

    map_parser = subparsers.add_parser("map", help="print source-to-target layout mappings")
    add_common_options(map_parser)
    map_parser.add_argument("--json", action="store_true", help="print JSON manifest")
    map_parser.set_defaults(func=command_map)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
