#!/usr/bin/env python3
"""Validate maintained Python changes with Mypy and Pyrefly.

The validator compares structured diagnostics at a Git comparison base with
diagnostics from the current working tree. It retains no checked-in diagnostic
baseline: unchanged legacy diagnostics outside changed declarations do not
block a change, while new diagnostics and diagnostics inside changed
declarations do.

Mypy's own incremental base cache survives between invocations in the worktree's
Git directory. Current analysis uses a disposable copy of that cache and shadow
copies of edited sources so timestamp-preserving edits are still revalidated.

By default, the validator compares the current branch with
``refs/heads/main``. Pass ``--base <revision>`` only when deliberately
inspecting a different change boundary. Both snapshots resolve the src-layout
package from their own source tree, including before typing was configured.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_COMPARISON_REF = "refs/heads/main"
CONFIG_PATHS = frozenset({"mypy.ini", "pyrefly.toml", "pyproject.toml"})
IGNORED_PYTHON_PREFIXES = ("tickets/", ".codex/", ".agents")
REMOVED_SUBPROCESS_ENV = (
    "GIT_DIR",
    "GIT_INDEX_FILE",
    "GIT_WORK_TREE",
    "MYPY_CACHE_DIR",
    "MYPYPATH",
    "PYREFLY_CONFIG",
    "PYTHONPATH",
)


class ValidationSetupError(RuntimeError):
    """The comparison or checker environment could not be established."""


@dataclass(frozen=True, order=True)
class Scope:
    """One independently invokable Python analysis scope."""

    label: str
    target: str
    mypy_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Diagnostic:
    """One normalized type-checker error."""

    checker: str
    scope: str
    path: str
    line: int
    column: int
    code: str
    message: str
    source_line: str

    @property
    def fingerprint(self) -> tuple[str, str, str, str, str]:
        """Return a line-shift-stable identity for change comparison."""
        return (self.scope, self.path, self.code, self.message, self.source_line)


@dataclass(frozen=True)
class CheckerRun:
    """All diagnostics from one checker at one repository state."""

    diagnostics: tuple[Diagnostic, ...]
    failure: str | None = None


@dataclass(frozen=True)
class ReportedDiagnostic:
    """A current diagnostic together with why it blocks the change."""

    diagnostic: Diagnostic
    reason: str


@dataclass(frozen=True)
class SourceChanges:
    """Changed current lines and lines removed without replacement."""

    current_lines: frozenset[int]
    deleted_base_lines: frozenset[int]


@dataclass(frozen=True)
class Declaration:
    """One source declaration with a stable deletion-matching identity."""

    identity: tuple[str, ...]
    start: int
    end: int


def _subprocess_environment() -> dict[str, str]:
    """Return a validation environment without ambient Git/checker injection."""
    environment = os.environ.copy()
    for name in REMOVED_SUBPROCESS_ENV:
        environment.pop(name, None)
    return environment


def _run_git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only Git command from the repository root."""
    git = shutil.which("git", path=os.environ.get("PATH"))
    if git is None:
        raise ValidationSetupError("executable not found: git")
    try:
        completed = subprocess.run(
            (git, *arguments),
            cwd=REPOSITORY_ROOT,
            env=_subprocess_environment(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise ValidationSetupError(f"could not start git: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = completed.stderr.decode(errors="replace").strip()
        command = "git " + " ".join(arguments)
        raise ValidationSetupError(
            f"{command} exited {completed.returncode}: {diagnostic}"
        )
    return completed


def _resolve_comparison_base(requested: str | None) -> tuple[str, str]:
    """Resolve a ref and its merge base with the current committed HEAD."""
    reference = requested if requested is not None else DEFAULT_COMPARISON_REF
    try:
        resolved = _run_git(
            "rev-parse",
            "--verify",
            f"{reference}^{{commit}}",
        ).stdout.strip()
    except ValidationSetupError as exc:
        if requested is None:
            raise ValidationSetupError(
                f"canonical comparison ref {DEFAULT_COMPARISON_REF} is "
                "unavailable; pass --base <revision> only for an explicitly "
                "different boundary"
            ) from exc
        raise
    resolved_text = resolved.decode("ascii", errors="strict")
    merge_base = _run_git("merge-base", "HEAD", resolved_text).stdout.strip()
    return reference, merge_base.decode("ascii", errors="strict")


def _nul_paths(output: bytes) -> tuple[str, ...]:
    """Decode Git's NUL-delimited repository-relative paths."""
    return tuple(os.fsdecode(item) for item in output.split(b"\0") if item)


def _changed_paths(base: str) -> tuple[str, ...]:
    """Return committed, staged, unstaged, and untracked paths since base."""
    changed = set(
        _nul_paths(_run_git("diff", "--name-only", "-z", base, "--").stdout)
    )
    changed.update(
        _nul_paths(
            _run_git("ls-files", "--others", "--exclude-standard", "-z").stdout
        )
    )
    return tuple(sorted(changed))


def _is_maintained_python(path: str) -> bool:
    """Return whether change-relative validation owns this Python path."""
    if not path.endswith(".py"):
        return False
    return not path.startswith(IGNORED_PYTHON_PREFIXES)


def _scope_for_other_path(path: str) -> Scope:
    """Return a stable scope for a maintained Python path outside core roots."""
    parts = Path(path).parts
    if len(parts) >= 2 and parts[0] == "experiments":
        target = Path(parts[0], parts[1]).as_posix()
        return Scope(target, target, ("--explicit-package-bases",))
    return Scope(path, path, ("--explicit-package-bases",))


def _repository_maintained_python_paths() -> tuple[str, ...]:
    """Return tracked and untracked maintained Python paths in this checkout."""
    paths = _nul_paths(
        _run_git(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
    )
    return tuple(path for path in paths if _is_maintained_python(path))


def _analysis_scopes(changed_paths: tuple[str, ...]) -> tuple[Scope, ...]:
    """Derive cohesive checker scopes from the changed maintained files."""
    core = {
        "src": Scope("src", "src", ("--explicit-package-bases",)),
        "tests": Scope("tests", "tests", ("--explicit-package-bases",)),
        "scripts": Scope("scripts", "scripts", ("--explicit-package-bases",)),
    }
    maintained_paths = _repository_maintained_python_paths()
    scopes: set[Scope] = set()
    if CONFIG_PATHS.intersection(changed_paths):
        scope_paths = tuple(sorted(set(changed_paths).union(maintained_paths)))
    else:
        scope_paths = changed_paths

    for path in scope_paths:
        if not _is_maintained_python(path):
            continue
        top_level = Path(path).parts[0]
        if top_level == "src":
            # Package and standalone probes share src-layout imports. Check
            # maintained consumers when the production contract changes.
            scopes.update(core.values())
            scopes.update(
                _scope_for_other_path(consumer)
                for consumer in maintained_paths
                if Path(consumer).parts[0] not in core
            )
        elif top_level == "scripts":
            scopes.update((core["scripts"], core["tests"]))
        elif top_level in core:
            scopes.add(core[top_level])
        else:
            scopes.add(_scope_for_other_path(path))
    return tuple(sorted(scopes))


def _materialize_base(base: str, destination: Path) -> None:
    """Extract the committed comparison tree without registering a worktree."""
    destination.mkdir(parents=True)
    archive_path = destination.parent / "base.tar"
    _run_git(
        "archive",
        "--format=tar",
        f"--output={archive_path}",
        base,
    )
    try:
        with tarfile.open(archive_path, mode="r") as archive:
            for member in archive:
                if member.issym() or member.islnk():
                    continue
                target = (destination / member.name).resolve()
                if not target.is_relative_to(destination.resolve()):
                    raise tarfile.ExtractError(f"archive member escapes base: {member.name}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise tarfile.ExtractError(f"archive member has no data: {member.name}")
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(member.mode & 0o777)
                    os.utime(target, (member.mtime, member.mtime))
                else:
                    raise tarfile.ExtractError(f"unsupported archive member: {member.name}")
    except (OSError, tarfile.TarError) as exc:
        raise ValidationSetupError(
            f"could not materialize comparison base {base}: {exc}"
        ) from exc

def _read_lines(path: Path) -> list[str]:
    """Read source lines while retaining malformed text for checker reporting."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        # Added and removed files legitimately have no counterpart in one tree.
        return []


def _source_changes(base_path: Path, current_path: Path) -> SourceChanges:
    """Return current edits and base lines removed without replacement."""
    before = _read_lines(base_path)
    after = _read_lines(current_path)
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    current_lines: set[int] = set()
    deleted_base_lines: set[int] = set()
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if after_start != after_end:
            current_lines.update(range(after_start + 1, after_end + 1))
        elif before_start != before_end:
            deleted_base_lines.update(range(before_start + 1, before_end + 1))
    return SourceChanges(
        current_lines=frozenset(current_lines),
        deleted_base_lines=frozenset(deleted_base_lines),
    )


def _node_start(node: ast.AST) -> int:
    """Return a definition's first decorator or declaration line."""
    start = node.lineno if isinstance(node, (ast.stmt, ast.expr)) else 1
    decorators = (
        node.decorator_list
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        else ()
    )
    decorator_lines = [decorator.lineno for decorator in decorators]
    return min((start, *decorator_lines))


class _DeclarationCollector(ast.NodeVisitor):
    """Collect definitions and module-scope declarations from an AST."""

    def __init__(self) -> None:
        self.declarations: list[Declaration] = []
        self._scope: list[str] = []

    def _record(self, node: ast.AST, identity: tuple[str, ...]) -> None:
        end = node.end_lineno if isinstance(node, (ast.stmt, ast.expr)) else None
        if isinstance(end, int):
            self.declarations.append(
                Declaration(identity=identity, start=_node_start(node), end=end)
            )

    def _visit_named_scope(self, node: ast.AST, kind: str, name: str) -> None:
        identity = (*self._scope, kind, name)
        self._record(node, identity)
        self._scope.extend((kind, name))
        self.generic_visit(node)
        del self._scope[-2:]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Record and traverse a synchronous function declaration."""
        self._visit_named_scope(node, "function", node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Record and traverse an asynchronous function declaration."""
        self._visit_named_scope(node, "async-function", node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Record and traverse a class declaration."""
        self._visit_named_scope(node, "class", node.name)

    def _record_module_declaration(
        self,
        node: ast.AST,
        kind: str,
        names: tuple[str, ...],
    ) -> None:
        if not self._scope:
            self._record(node, ("module", kind, *names))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Record a module-scope assignment declaration."""
        names = tuple(
            sorted(
                name
                for target in node.targets
                for name in _assignment_target_names(target)
            )
        )
        self._record_module_declaration(node, "assignment", names)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Record a module-scope annotated assignment declaration."""
        names = tuple(sorted(_assignment_target_names(node.target)))
        self._record_module_declaration(node, "annotated-assignment", names)

    def visit_Import(self, node: ast.Import) -> None:
        """Record a module-scope import declaration."""
        imports = tuple(
            sorted(
                f"{alias.name}->{alias.asname or alias.name.partition('.')[0]}"
                for alias in node.names
            )
        )
        self._record_module_declaration(node, "import", imports)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """Record a module-scope from-import declaration."""
        module = "." * node.level + (node.module or "")
        imports = tuple(
            sorted(f"{alias.name}->{alias.asname or alias.name}" for alias in node.names)
        )
        self._record_module_declaration(node, "from-import", (module, *imports))


def _assignment_target_names(target: ast.expr) -> tuple[str, ...]:
    """Return stable names bound by an assignment target."""
    if isinstance(target, ast.Name):
        return (target.id,)
    if isinstance(target, ast.Starred):
        return _assignment_target_names(target.value)
    if isinstance(target, (ast.List, ast.Tuple)):
        return tuple(
            name for element in target.elts for name in _assignment_target_names(element)
        )
    return (ast.unparse(target),)


def _collect_declarations(tree: ast.AST) -> tuple[Declaration, ...]:
    """Return all declarations that own affected source regions."""
    collector = _DeclarationCollector()
    collector.visit(tree)
    return tuple(collector.declarations)


def _smallest_enclosing_declaration(
    line: int,
    declarations: tuple[Declaration, ...],
) -> Declaration | None:
    """Return the narrowest declaration containing one source line."""
    enclosing = [
        declaration
        for declaration in declarations
        if declaration.start <= line <= declaration.end
    ]
    if not enclosing:
        return None
    return min(enclosing, key=lambda declaration: declaration.end - declaration.start)


def _expand_current_lines(
    lines: frozenset[int],
    declarations: tuple[Declaration, ...],
) -> set[tuple[int, int]]:
    """Expand edited current lines to their owning declarations."""
    expanded: set[tuple[int, int]] = set()
    for line in lines:
        declaration = _smallest_enclosing_declaration(line, declarations)
        expanded.add(
            (line, line)
            if declaration is None
            else (declaration.start, declaration.end)
        )
    return expanded


def _deletion_regions(
    deleted_lines: frozenset[int],
    base_declarations: tuple[Declaration, ...],
    current_declarations: tuple[Declaration, ...],
) -> set[tuple[int, int]]:
    """Map deletion-touched declarations that still exist into current source."""
    if not deleted_lines:
        return set()
    base_groups: dict[tuple[str, ...], list[Declaration]] = {}
    current_groups: dict[tuple[str, ...], list[Declaration]] = {}
    for declaration in base_declarations:
        base_groups.setdefault(declaration.identity, []).append(declaration)
    for declaration in current_declarations:
        current_groups.setdefault(declaration.identity, []).append(declaration)

    regions: set[tuple[int, int]] = set()
    for identity, base_group in base_groups.items():
        current_group = current_groups.get(identity, [])
        # Equal cardinality distinguishes an edited surviving declaration from
        # a removed declaration whose successor merely follows the deletion.
        if len(base_group) != len(current_group):
            continue
        for index, base_declaration in enumerate(base_group):
            if any(
                base_declaration.start <= line <= base_declaration.end
                for line in deleted_lines
            ):
                current_declaration = current_group[index]
                regions.add((current_declaration.start, current_declaration.end))
    return regions


def _changed_declaration_regions(
    base_root: Path,
    changed_paths: tuple[str, ...],
) -> dict[str, tuple[tuple[int, int], ...]]:
    """Expand changed lines to their smallest enclosing declaration."""
    regions: dict[str, tuple[tuple[int, int], ...]] = {}
    for relative_path in changed_paths:
        if not _is_maintained_python(relative_path):
            continue
        current_path = REPOSITORY_ROOT / relative_path
        if not current_path.is_file():
            continue
        changes = _source_changes(
            base_root / relative_path,
            current_path,
        )
        if not changes.current_lines and not changes.deleted_base_lines:
            continue
        source = current_path.read_text(encoding="utf-8", errors="replace")
        try:
            current_tree = ast.parse(source, filename=relative_path)
        except SyntaxError:
            regions[relative_path] = tuple(
                (line, line) for line in sorted(changes.current_lines)
            )
            continue
        current_declarations = _collect_declarations(current_tree)
        expanded = _expand_current_lines(
            changes.current_lines,
            current_declarations,
        )
        base_source = (
            (base_root / relative_path).read_text(
                encoding="utf-8",
                errors="replace",
            )
            if (base_root / relative_path).is_file()
            else ""
        )
        try:
            base_tree = ast.parse(base_source, filename=relative_path)
        except SyntaxError:
            base_tree = None
        if base_tree is not None:
            expanded.update(
                _deletion_regions(
                    changes.deleted_base_lines,
                    _collect_declarations(base_tree),
                    current_declarations,
                )
            )
        regions[relative_path] = tuple(sorted(expanded))
    return regions


def _resolve_checker(name: str) -> str | None:
    """Resolve a checker from the repository environment, then ambient PATH."""
    relative = Path("Scripts", f"{name}.exe") if os.name == "nt" else Path("bin", name)
    candidate = REPOSITORY_ROOT / ".venv" / relative
    if candidate.is_file():
        return str(candidate)
    return shutil.which(name, path=os.environ.get("PATH"))


def _checker_cache_root(base: str) -> Path:
    """Retain checker-owned incremental state privately for this worktree."""
    git_directory = os.fsdecode(
        _run_git("rev-parse", "--absolute-git-dir").stdout.rstrip(b"\n")
    )
    cache_root = Path(git_directory) / "acp-proxy-typecheck-cache" / base
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ValidationSetupError(f"could not prepare checker cache: {exc}") from exc
    return cache_root


def _mypy_shadow_files(
    base_root: Path,
    changed_paths: tuple[str, ...],
    destination: Path,
) -> tuple[tuple[Path, Path], ...]:
    """Force content checks of edited sources without touching checkout mtimes."""
    shadows: list[tuple[Path, Path]] = []
    for relative_path in changed_paths:
        current = REPOSITORY_ROOT / relative_path
        if current.suffix not in (".py", ".pyi") or not current.is_file():
            continue
        before = base_root / relative_path
        if not before.is_file():
            continue
        before_stat = before.stat()
        current_stat = current.stat()
        if (
            current_stat.st_size != before_stat.st_size
            or int(current_stat.st_mtime) != int(before_stat.st_mtime)
        ):
            continue
        shadow = destination / relative_path
        shadow.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current, shadow)
        # Mypy trusts equal sizes and whole-second mtimes. Its seeded cache
        # describes the base, so a distinct shadow mtime forces hashing.
        stamp = before_stat.st_mtime_ns + 1_000_000_000
        os.utime(shadow, ns=(stamp, stamp))
        shadows.append((current, shadow))
    return tuple(shadows)


def _checker_python(executable: str) -> str:
    """Return one interpreter for identical base/current import discovery."""
    checker_directory = Path(executable).resolve().parent
    sibling_name = "python.exe" if os.name == "nt" else "python"
    sibling = checker_directory / sibling_name
    if sibling.is_file():
        return str(sibling)
    relative = (
        Path("Scripts", "python.exe") if os.name == "nt" else Path("bin", "python")
    )
    repository_python = REPOSITORY_ROOT / ".venv" / relative
    return str(repository_python) if repository_python.is_file() else sys.executable


def _json_object(text: str) -> dict[str, object]:
    """Decode one JSON object without allowing an untyped result to escape."""
    decoded: object = json.loads(text)
    if not isinstance(decoded, dict):
        raise ValueError("expected a JSON object")
    result: dict[str, object] = {}
    for key, value in decoded.items():
        if not isinstance(key, str):
            raise ValueError("JSON object keys must be strings")
        result[key] = value
    return result


def _required_string(value: object, field: str) -> str:
    """Return a required string field from structured checker output."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value


def _integer(value: object, default: int = 0) -> int:
    """Return a checker position without accepting booleans as integers."""
    return value if type(value) is int else default


def _normalize_path(root: Path, raw_path: str) -> str:
    """Normalize one checker path to repository-relative POSIX spelling."""
    path = Path(raw_path)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _source_line(
    root: Path,
    relative_path: str,
    line: int,
    source_lines: dict[str, list[str]],
) -> str:
    """Read the diagnostic's source line for line-shift-stable comparison."""
    if line < 1:
        return ""
    if relative_path not in source_lines:
        source_lines[relative_path] = _read_lines(root / relative_path)
    lines = source_lines[relative_path]
    if line > len(lines):
        return ""
    return lines[line - 1].strip()


def _diagnostic(
    *,
    checker: str,
    scope: Scope,
    root: Path,
    path: str,
    line: int,
    column: int,
    code: str,
    message: str,
    source_lines: dict[str, list[str]],
) -> Diagnostic:
    """Construct one normalized diagnostic."""
    normalized_path = _normalize_path(root, path)
    normalized_message = message.replace(str(root), "<ROOT>")
    return Diagnostic(
        checker=checker,
        scope=scope.label,
        path=normalized_path,
        line=line,
        column=column,
        code=code,
        message=normalized_message,
        source_line=_source_line(root, normalized_path, line, source_lines),
    )


def _parse_mypy(stdout: str, root: Path, scope: Scope) -> tuple[Diagnostic, ...]:
    """Parse Mypy's JSON-lines error output."""
    diagnostics: list[Diagnostic] = []
    source_lines: dict[str, list[str]] = {}
    for line_text in stdout.splitlines():
        if not line_text.strip():
            continue
        item = _json_object(line_text)
        if item.get("severity") != "error":
            continue
        raw_code = item.get("code")
        code = raw_code if isinstance(raw_code, str) else "unknown"
        diagnostics.append(
            _diagnostic(
                checker="mypy",
                scope=scope,
                root=root,
                path=_required_string(item.get("file"), "file"),
                line=_integer(item.get("line"), -1),
                column=_integer(item.get("column")),
                code=code,
                message=_required_string(item.get("message"), "message"),
                source_lines=source_lines,
            )
        )
    return tuple(diagnostics)


def _object_list(value: object, field: str) -> tuple[dict[str, object], ...]:
    """Validate a list of JSON objects."""
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    items: list[dict[str, object]] = []
    for raw_item in value:
        if not isinstance(raw_item, dict):
            raise ValueError(f"{field} entries must be objects")
        item: dict[str, object] = {}
        for key, entry in raw_item.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} object keys must be strings")
            item[key] = entry
        items.append(item)
    return tuple(items)


def _parse_pyrefly(
    stdout: str,
    root: Path,
    scope: Scope,
) -> tuple[Diagnostic, ...]:
    """Parse Pyrefly's JSON error output."""
    payload = _json_object(stdout)
    diagnostics: list[Diagnostic] = []
    source_lines: dict[str, list[str]] = {}
    for item in _object_list(payload.get("errors"), "errors"):
        if item.get("severity") != "error":
            continue
        raw_code = item.get("name")
        code = raw_code if isinstance(raw_code, str) else "unknown"
        message_value = item.get("concise_description", item.get("description"))
        diagnostics.append(
            _diagnostic(
                checker="pyrefly",
                scope=scope,
                root=root,
                path=_required_string(item.get("path"), "path"),
                line=_integer(item.get("line"), -1),
                column=_integer(item.get("column")),
                code=code,
                message=_required_string(message_value, "description"),
                source_lines=source_lines,
            )
        )
    return tuple(diagnostics)


def _pyrefly_bootstrap_arguments(root: Path) -> tuple[str, ...]:
    """Establish analysis for old snapshots without overriding their settings."""
    standalone = root / "pyrefly.toml"
    project = root / "pyproject.toml"
    configuration: object = None
    if standalone.is_file():
        configuration = tomllib.loads(standalone.read_text(encoding="utf-8"))
    elif project.is_file():
        parsed = tomllib.loads(project.read_text(encoding="utf-8"))
        tool: object = parsed.get("tool")
        if isinstance(tool, dict):
            configuration = tool.get("pyrefly")
    if configuration is None:
        # An unconfigured checker uses basic, which omits established errors
        # and would misclassify them as new when configuration is introduced.
        return (
            "--preset", "legacy",
            "--error", "no-any-return",
            "--warn", "redundant-cast",
            "--python-version", "3.11",
            "--search-path", str(root / "src"),
            "--search-path", str(root),
        )
    if isinstance(configuration, dict) and not (
        "search_path" in configuration or "search-path" in configuration
    ):
        return ("--search-path", str(root / "src"), "--search-path", str(root))
    return ()


def _run_checker_scope(
    checker: str,
    executable: str,
    root: Path,
    scope: Scope,
    cache_directory: Path,
    shadow_files: tuple[tuple[Path, Path], ...] = (),
) -> CheckerRun:
    """Run and parse one checker scope at one repository state."""
    if not (root / scope.target).exists():
        return CheckerRun(())
    environment = _subprocess_environment()
    # Resolve from this snapshot's cwd before editable installations. Keep paths
    # relative so Mypy's cached imported diagnostics can move between snapshots.
    environment["MYPYPATH"] = "src"
    if checker == "mypy":
        shadow_arguments = tuple(
            argument
            for current, shadow in shadow_files
            for argument in ("--shadow-file", str(current), str(shadow))
        )
        if shadow_arguments:
            # Response files keep large edits within Windows' command-line limit.
            argument_file = cache_directory / "shadow-arguments.txt"
            argument_file.write_text("\n".join(shadow_arguments) + "\n", encoding="utf-8")
            shadow_arguments = (f"@{argument_file}",)
        argv = (
            executable,
            "--strict",
            "--python-version", "3.11",
            "--output=json",
            "--no-pretty",
            "--python-executable",
            _checker_python(executable),
            "--cache-dir",
            str(cache_directory),
            *scope.mypy_flags,
            *shadow_arguments,
            scope.target,
        )
    else:
        try:
            bootstrap = _pyrefly_bootstrap_arguments(root)
        except (OSError, ValueError) as exc:
            return CheckerRun((), f"could not read Pyrefly configuration: {exc}")
        argv = (
            executable,
            "check",
            "--output-format",
            "json",
            "--python-interpreter-path",
            _checker_python(executable),
            *bootstrap,
            scope.target,
        )
    try:
        completed = subprocess.run(
            argv,
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return CheckerRun((), f"could not start {checker}: {exc}")

    if completed.returncode not in (0, 1):
        diagnostic = (completed.stderr or completed.stdout).strip()
        return CheckerRun(
            (),
            f"{checker} could not establish a diagnostic result for "
            f"{scope.label} (exit {completed.returncode}): {diagnostic}",
        )
    try:
        diagnostics = (
            _parse_mypy(completed.stdout, root, scope)
            if checker == "mypy"
            else _parse_pyrefly(completed.stdout, root, scope)
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return CheckerRun(
            (),
            f"{checker} returned invalid structured output for {scope.label}: {exc}",
        )
    if completed.returncode == 1 and not diagnostics:
        diagnostic = (completed.stderr or completed.stdout).strip()
        return CheckerRun(
            (),
            f"{checker} exited 1 without error diagnostics for {scope.label}: "
            f"{diagnostic}",
        )
    return CheckerRun(diagnostics)


def _run_checker(
    checker: str,
    root: Path,
    scopes: tuple[Scope, ...],
    cache_root: Path,
    shadow_files: tuple[tuple[Path, Path], ...] = (),
) -> CheckerRun:
    """Run one checker over every derived scope."""
    executable = _resolve_checker(checker)
    if executable is None:
        return CheckerRun(
            (),
            f"{checker} executable not found; run the repository environment sync",
        )
    diagnostics: list[Diagnostic] = []
    for scope in scopes:
        scope_cache = cache_root / checker / scope.label.replace("/", "-")
        if checker == "mypy":
            scope_cache.mkdir(parents=True, exist_ok=True)
        result = _run_checker_scope(
            checker,
            executable,
            root,
            scope,
            scope_cache,
            shadow_files,
        )
        if result.failure is not None:
            return result
        diagnostics.extend(result.diagnostics)
    return CheckerRun(tuple(diagnostics))


def _intersects_changed_declaration(
    diagnostic: Diagnostic,
    regions: dict[str, tuple[tuple[int, int], ...]],
) -> bool:
    """Return whether a diagnostic lies in a changed declaration."""
    return any(
        start <= diagnostic.line <= end
        for start, end in regions.get(diagnostic.path, ())
    )


def _blocking_diagnostics(
    base: tuple[Diagnostic, ...],
    current: tuple[Diagnostic, ...],
    regions: dict[str, tuple[tuple[int, int], ...]],
) -> tuple[ReportedDiagnostic, ...]:
    """Select new errors and existing errors on changed declarations."""
    remaining = Counter(diagnostic.fingerprint for diagnostic in base)
    reported: list[ReportedDiagnostic] = []
    reported_indexes: set[int] = set()
    for index, diagnostic in enumerate(current):
        if remaining[diagnostic.fingerprint] > 0:
            remaining[diagnostic.fingerprint] -= 1
            continue
        reported.append(ReportedDiagnostic(diagnostic, "new diagnostic"))
        reported_indexes.add(index)
    for index, diagnostic in enumerate(current):
        if index in reported_indexes:
            continue
        if _intersects_changed_declaration(diagnostic, regions):
            reported.append(
                ReportedDiagnostic(
                    diagnostic,
                    "existing diagnostic remains in affected declaration",
                )
            )
    return tuple(reported)


def _print_checker_result(
    checker: str,
    base: CheckerRun,
    current: CheckerRun,
    regions: dict[str, tuple[tuple[int, int], ...]],
) -> bool:
    """Print one checker verdict and return whether it passed."""
    failure = base.failure or current.failure
    if failure is not None:
        print(f"[ FAIL  ] {checker} — {failure}", flush=True)
        return False
    blocking = _blocking_diagnostics(base.diagnostics, current.diagnostics, regions)
    if not blocking:
        print(
            f"[ PASS  ] {checker} — 0 new or affected-surface diagnostics "
            f"({len(current.diagnostics)} existing outside the changed surface)",
            flush=True,
        )
        return True
    print(
        f"[ FAIL  ] {checker} — {len(blocking)} new or affected-surface "
        "diagnostics",
        flush=True,
    )
    for reported in blocking:
        diagnostic = reported.diagnostic
        print(
            f"    {diagnostic.path}:{diagnostic.line}:{diagnostic.column}: "
            f"{diagnostic.message} [{diagnostic.code}] "
            f"({reported.reason})",
            flush=True,
        )
    return False


def _configure_console_output() -> None:
    """Keep diagnostics printable under strict redirected platform codepages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")


def main() -> int:
    """Run change-relative Mypy and Pyrefly validation."""
    _configure_console_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        help=(
            "comparison revision; defaults to the canonical local branch "
            f"{DEFAULT_COMPARISON_REF}"
        ),
    )
    arguments = parser.parse_args()
    try:
        reference, base = _resolve_comparison_base(arguments.base)
        changed_paths = _changed_paths(base)
        scopes = _analysis_scopes(changed_paths)
    except ValidationSetupError as exc:
        parser.error(str(exc))

    maintained_python = tuple(
        path for path in changed_paths if _is_maintained_python(path)
    )
    if not scopes:
        print(
            f"type validation: no maintained Python changes relative to {reference} ({base})",
            flush=True,
        )
        return 0

    print(
        f"type validation: {len(maintained_python)} maintained Python path(s) "
        f"relative to {reference} ({base}); scopes: "
        + ", ".join(scope.label for scope in scopes),
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="acp-proxy-typecheck-change-") as temp:
        temp_root = Path(temp)
        base_root = temp_root / "base"
        try:
            _materialize_base(base, base_root)
            regions = _changed_declaration_regions(base_root, changed_paths)
            cache_root = _checker_cache_root(base)
        except ValidationSetupError as exc:
            print(f"[ FAIL  ] setup — {exc}", flush=True)
            print("summary: 0 passed, 1 failed", flush=True)
            return 1

        results: list[bool] = []
        for checker in ("mypy", "pyrefly"):
            base_result = _run_checker(
                checker,
                base_root,
                scopes,
                cache_root / "base",
            )
            current_cache = temp_root / "cache" / "current"
            try:
                shadow_files: tuple[tuple[Path, Path], ...] = ()
                if checker == "mypy" and base_result.failure is None:
                    shutil.copytree(cache_root / "base" / checker, current_cache / checker)
                    shadow_files = _mypy_shadow_files(
                        base_root, changed_paths, temp_root / "shadow",
                    )
                current_result = _run_checker(
                    checker,
                    REPOSITORY_ROOT,
                    scopes,
                    current_cache,
                    shadow_files,
                )
            except OSError as exc:
                current_result = CheckerRun((), f"could not prepare checker cache: {exc}")
            results.append(
                _print_checker_result(
                    checker,
                    base_result,
                    current_result,
                    regions,
                )
            )

    failures = results.count(False)
    print(
        f"summary: {len(results) - failures} passed, {failures} failed",
        flush=True,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
