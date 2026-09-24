"""Session-owned file and command effects behind generic native client tools.

File paths are checked beneath the admitted workspace at execution. This is an
application path policy, not an OS sandbox against concurrent local processes.
Commands retain the process user's authority and may affect other paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import TypeAlias, assert_never

from .json_types import JsonObject, JsonValue, json_object, json_text, parse_json
from .owned_commands import (
    CommandCancelled,
    CommandResult,
    CommandSpec,
    OwnedCommands,
    ShellSpec,
)
from .permission_handler import PermissionDecision, PermissionHandler
from .permission_policy import PermissionAction, PermissionPolicy

MAX_FILE_BYTES = 4_000_000
MAX_BATCH_FILES = 64
MAX_FILE_EDITS = 128


class ToolArgumentError(ValueError):
    """A client tool invocation did not match its admitted input contract."""


def _text(value: object, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ToolArgumentError(
            f"{label} must be {'a nonempty' if nonempty else 'a'} string"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ToolArgumentError(f"{label} must be valid UTF-8 text") from None
    if len(encoded) > MAX_FILE_BYTES:
        raise ToolArgumentError(f"{label} exceeds {MAX_FILE_BYTES} UTF-8 bytes")
    return value


def _path_text(value: object) -> str:
    value = _text(value, "path", nonempty=True)
    if "\0" in value:
        raise ToolArgumentError("path must not contain a NUL character")
    return value


@dataclass(frozen=True)
class NewFile:
    """One exclusive file creation with exact UTF-8 content."""

    path: str
    content: str

    def __post_init__(self) -> None:
        _path_text(self.path)
        _text(self.content, "content")


@dataclass(frozen=True)
class ExactEdit:
    """Replace one unambiguous occurrence without whitespace normalization."""

    old_text: str
    new_text: str

    def __post_init__(self) -> None:
        _text(self.old_text, "old_text", nonempty=True)
        _text(self.new_text, "new_text")


@dataclass(frozen=True)
class EditedFile:
    """An ordered sequence of compatible edits published as one file change."""

    path: str
    edits: tuple[ExactEdit, ...]

    def __post_init__(self) -> None:
        _path_text(self.path)
        if (
            not isinstance(self.edits, tuple)
            or not 1 <= len(self.edits) <= MAX_FILE_EDITS
        ):
            raise ToolArgumentError(
                f"edits must contain 1 to {MAX_FILE_EDITS} replacements"
            )
        if not all(isinstance(item, ExactEdit) for item in self.edits):
            raise ToolArgumentError("edits must contain checked exact replacements")


def _batch(paths: tuple[str, ...]) -> None:
    if not 1 <= len(paths) <= MAX_BATCH_FILES:
        raise ToolArgumentError(f"files must contain 1 to {MAX_BATCH_FILES} entries")
    if len(set(paths)) != len(paths):
        raise ToolArgumentError("a batch may name each file only once")


@dataclass(frozen=True)
class CreateFiles:
    """An ordered, non-transactional batch of exclusive file creations."""

    files: tuple[NewFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple) or not all(
            isinstance(item, NewFile) for item in self.files
        ):
            raise ToolArgumentError("files must contain checked file creations")
        _batch(tuple(item.path for item in self.files))


@dataclass(frozen=True)
class EditFiles:
    """An ordered, non-transactional batch of exact file edits."""

    files: tuple[EditedFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple) or not all(
            isinstance(item, EditedFile) for item in self.files
        ):
            raise ToolArgumentError("files must contain checked file edits")
        _batch(tuple(item.path for item in self.files))


@dataclass(frozen=True)
class RunCommand:
    """One bounded noninteractive shell command; cwd remains workspace-scoped."""

    command: str
    cwd: str
    timeout_s: float

    def __post_init__(self) -> None:
        _text(self.command, "command", nonempty=True)
        if "\0" in self.command:
            raise ToolArgumentError("command must not contain a NUL character")
        _path_text(self.cwd)
        if (
            isinstance(self.timeout_s, bool)
            or not math.isfinite(self.timeout_s)
            or not 0 < self.timeout_s <= 300
        ):
            raise ToolArgumentError(
                "timeout must be finite, greater than zero and at most 300 seconds"
            )


WorkspaceOperation: TypeAlias = CreateFiles | EditFiles | RunCommand


def _action(operation: WorkspaceOperation) -> PermissionAction:
    if isinstance(operation, CreateFiles):
        return PermissionAction.CREATE
    if isinstance(operation, EditFiles):
        return PermissionAction.EDIT
    if isinstance(operation, RunCommand):
        return PermissionAction.COMMAND
    assert_never(operation)


@dataclass(frozen=True)
class ToolFailure:
    """A tool failure with the already-completed effects retained separately."""

    code: str
    message: str

    def to_json(self) -> JsonObject:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class FileReceipt:
    """One published file effect, with exact before/after byte digests."""

    action: PermissionAction
    path: str
    before_sha256: str | None
    after_sha256: str

    def to_json(self) -> JsonObject:
        return {
            "action": self.action.value,
            "path": self.path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
        }


@dataclass(frozen=True)
class FileOutcome:
    """Observed file effects; no rollback or batch atomicity is implied."""

    receipts: tuple[FileReceipt, ...]


@dataclass(frozen=True)
class CommandOutcome:
    """Settled command output; arbitrary command effects are not enumerated."""

    operation: RunCommand
    result: CommandResult


@dataclass(frozen=True)
class WorkspaceResult:
    """Immutable completion evidence for either file work or a command."""

    outcome: FileOutcome | CommandOutcome
    error: ToolFailure | None = None

    @property
    def receipts(self) -> tuple[FileReceipt, ...]:
        return self.outcome.receipts if isinstance(self.outcome, FileOutcome) else ()

    def to_json(self) -> JsonObject:
        """Serialize a tool result without claiming all command effects observed."""
        result: JsonObject = {
            "ok": self.error is None,
            "receipts": [receipt.to_json() for receipt in self.receipts],
            "error": self.error.to_json() if self.error is not None else None,
        }
        if isinstance(self.outcome, CommandOutcome):
            command = self.outcome.result
            result["command"] = {
                "cwd": self.outcome.operation.cwd,
                "stdout": command.stdout.decode("utf-8", errors="replace"),
                "stderr": command.stderr.decode("utf-8", errors="replace"),
                "stdout_truncated": command.stdout_truncated,
                "stderr_truncated": command.stderr_truncated,
                "output_decoding": "utf-8 with replacement",
                "exit_code": command.exit_code,
                "timed_out": command.timed_out,
                "filesystem_effects_enumerated": False,
            }
        return result

    def as_text(self) -> str:
        """Encode the structured callback result for the model."""
        return json_text(self.to_json())


class WorkspaceCancelled(asyncio.CancelledError):
    """Caller cancellation after owned work settles, including partial effects."""

    def __init__(self, result: WorkspaceResult) -> None:
        super().__init__("workspace execution cancelled after settlement")
        self.result = result


@dataclass(frozen=True)
class ToolDefinition:
    """Immutable registration definition; callers receive fresh schema objects."""

    name: str
    description: str
    schema_json: str
    confirmation_title: str
    confirmation_message: str

    def to_json(self) -> JsonObject:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": json_object(parse_json(self.schema_json)),
            "confirmationMessages": {
                "title": self.confirmation_title,
                "message": self.confirmation_message,
            },
        }


def _object(properties: JsonObject, required: list[str]) -> JsonObject:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _schema_definition(
    name: str,
    description: str,
    schema: JsonObject,
    *,
    confirmation_title: str,
    confirmation_message: str,
) -> ToolDefinition:
    return ToolDefinition(
        name,
        description,
        json_text(schema),
        confirmation_title,
        confirmation_message,
    )


def _fields(value: JsonValue, names: set[str]) -> JsonObject:
    if not isinstance(value, dict) or set(value) != names:
        raise ToolArgumentError(
            f"expected exactly these object fields: {', '.join(sorted(names))}"
        )
    return value


def _items(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ToolArgumentError("files and edits must be arrays")
    return value


class _FileFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.failure = ToolFailure(code, message)


class _PublishedFileCleanupError(_FileFailure):
    """The file effect committed but its staging path could not be removed."""


class WorkspaceTools:
    """Own a session's policy, current operation, file workers and commands."""

    def __init__(
        self,
        workspace: Path,
        policy: PermissionPolicy,
        *,
        env: Mapping[str, str] | None = None,
        shell: ShellSpec | None = None,
    ) -> None:
        if not workspace.is_absolute():
            raise ValueError("workspace must be an absolute path")
        self.workspace = workspace
        self._permission = PermissionHandler(policy)
        child_env = dict(os.environ if env is None else env)
        for key in tuple(child_env):
            if key.upper() in {
                "MEADOW_BRIDGE_MEADOW_SECRET",
                "MEADOW_BRIDGE_CONTAINER_BOUNDARY",
                "ACP_PROXY_MEADOW_SECRET",
                "ACP_PROXY_CONTAINER_BOUNDARY",
            }:
                del child_env[key]
        self._commands = OwnedCommands(child_env, shell=shell)
        self._active: asyncio.Task[WorkspaceResult] | None = None
        self._stop: threading.Event | None = None
        self._closed = False

    @staticmethod
    def definitions() -> tuple[ToolDefinition, ...]:
        """Register generic names; native canonical edit wrappers are avoided."""
        text: JsonObject = {"type": "string", "maxLength": MAX_FILE_BYTES}
        path: JsonObject = {
            "type": "string",
            "minLength": 1,
            "description": "Workspace-relative or absolute file path beneath the workspace.",
        }
        edit = _object(
            {
                "old_text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Exact text that must occur once, including whitespace and newlines.",
                },
                "new_text": {
                    **text,
                    "description": "Replacement text; an empty string deletes the matched text.",
                },
            },
            ["old_text", "new_text"],
        )
        create_file = _object(
            {
                "path": path,
                "content": {
                    **text,
                    "description": "Complete UTF-8 file content, preserving supplied whitespace and newlines.",
                },
            },
            ["path", "content"],
        )
        edit_file = _object(
            {
                "path": path,
                "edits": {
                    "type": "array",
                    "description": "Exact replacements applied in order to this file before publication.",
                    "items": edit,
                    "minItems": 1,
                    "maxItems": MAX_FILE_EDITS,
                },
            },
            ["path", "edits"],
        )
        return (
            _schema_definition(
                "workspace_create_files",
                "Create new UTF-8 files beneath the workspace. Parents must exist; existing files are never overwritten. Supply files with path/content. Files are applied in order, stop on first failure, and report completed effects; the batch is not atomic.",
                _object(
                    {
                        "files": {
                            "type": "array",
                            "description": "New files to create in order; stop at the first failure and report completed files.",
                            "items": create_file,
                            "minItems": 1,
                            "maxItems": MAX_BATCH_FILES,
                        }
                    },
                    ["files"],
                ),
                confirmation_title="Create workspace files",
                confirmation_message="Apply the session permission policy to the requested file creations.",
            ),
            _schema_definition(
                "workspace_edit_files",
                "Edit UTF-8 workspace files using exact old_text/new_text replacements. Each old_text must occur exactly once in the current file; whitespace and newlines are significant. Edits within a file apply in order and publish together. Files apply in order; partial effects are reported if a later file fails.",
                _object(
                    {
                        "files": {
                            "type": "array",
                            "description": "Existing files to edit in order; stop at the first failure and report completed files.",
                            "items": edit_file,
                            "minItems": 1,
                            "maxItems": MAX_BATCH_FILES,
                        }
                    },
                    ["files"],
                ),
                confirmation_title="Edit workspace files",
                confirmation_message="Apply the session permission policy to the requested exact file edits.",
            ),
            _schema_definition(
                "workspace_run_command",
                "Run a noninteractive foreground command in the configured shell (POSIX shell or Windows PowerShell). Do not launch detached background services. Supply command, workspace-relative or absolute cwd, and timeout in seconds (0 < timeout <= 300). Output is bounded with explicit truncation; timeout/cancellation settles the owned POSIX process group or Windows Job. Commands retain process-user authority and are not a filesystem sandbox.",
                _object(
                    {
                        "command": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Noninteractive foreground shell command; detached background services are unsupported.",
                        },
                        "cwd": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Existing working directory beneath the workspace, as a workspace-relative or absolute path.",
                        },
                        "timeout": {
                            "type": "number",
                            "description": "Maximum command duration in seconds, greater than zero and no more than 300.",
                            "exclusiveMinimum": 0,
                            "maximum": 300,
                        },
                    },
                    ["command", "cwd", "timeout"],
                ),
                confirmation_title="Run workspace command",
                confirmation_message="Apply the session permission policy to the requested shell command.",
            ),
        )

    def decode(self, name: str, arguments: JsonObject) -> WorkspaceOperation:
        """Admit raw callback arguments into immutable checked operations."""
        if name == "workspace_create_files":
            files = _items(_fields(arguments, {"files"})["files"])
            created = []
            for item in files:
                value = _fields(item, {"path", "content"})
                created.append(
                    NewFile(
                        _path_text(value["path"]), _text(value["content"], "content")
                    )
                )
            return CreateFiles(tuple(created))
        if name == "workspace_edit_files":
            files = _items(_fields(arguments, {"files"})["files"])
            edited = []
            for item in files:
                value = _fields(item, {"path", "edits"})
                replacements = []
                for raw_edit in _items(value["edits"]):
                    edit = _fields(raw_edit, {"old_text", "new_text"})
                    replacements.append(
                        ExactEdit(
                            _text(edit["old_text"], "old_text", nonempty=True),
                            _text(edit["new_text"], "new_text"),
                        )
                    )
                edited.append(
                    EditedFile(_path_text(value["path"]), tuple(replacements))
                )
            return EditFiles(tuple(edited))
        if name == "workspace_run_command":
            value = _fields(arguments, {"command", "cwd", "timeout"})
            timeout = value["timeout"]
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ToolArgumentError("timeout must be a number of seconds")
            try:
                seconds = float(timeout)
            except OverflowError:
                raise ToolArgumentError("timeout exceeds the supported range") from None
            return RunCommand(
                _text(value["command"], "command", nonempty=True),
                _path_text(value["cwd"]),
                seconds,
            )
        raise ToolArgumentError(f"unrecognized workspace tool: {name}")

    def confirm(self, operation: WorkspaceOperation) -> PermissionDecision:
        """Optional native confirmation; effects still authorize independently."""
        if self._closed:
            raise RuntimeError("workspace tools are closed")
        return self._permission.confirm(_action(operation))

    async def execute(self, operation: WorkspaceOperation) -> WorkspaceResult:
        """Execute at most one operation; settle owned work before returning."""
        if self._closed:
            raise RuntimeError("workspace tools are closed")
        if self._active is not None:
            raise RuntimeError("workspace tools already have an active operation")
        self._permission.authorize(_action(operation))
        stop = threading.Event()
        task = asyncio.create_task(self._execute_owned(operation, stop))
        self._active, self._stop = task, stop
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            result = await self._cancel_owned(task)
            raise WorkspaceCancelled(result) from None
        finally:
            self._active, self._stop = None, None

    async def cancel(self) -> None:
        """Stop the current operation and wait for every owned effect to settle."""
        task = self._active
        if task is not None:
            if self._stop is not None:
                self._stop.set()
            await self._cancel_owned(task)

    async def close(self) -> None:
        """Revoke admission, settle file work, and close command ownership."""
        self._closed = True
        try:
            await self.cancel()
        finally:
            await self._commands.close()

    async def _cancel_owned(
        self, task: asyncio.Task[WorkspaceResult]
    ) -> WorkspaceResult:
        try:
            await self._commands.cancel()
        except CommandCancelled:
            # Its execute caller still owns the captured result. Continue waiting
            # for that caller to fold the settled command into workspace evidence.
            pass
        return await self._settle(task)

    @staticmethod
    async def _settle(task: asyncio.Task[WorkspaceResult]) -> WorkspaceResult:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Repeated cancellation cannot abandon already submitted I/O.
                continue
        return task.result()

    async def _execute_owned(
        self, operation: WorkspaceOperation, stop: threading.Event
    ) -> WorkspaceResult:
        if isinstance(operation, RunCommand):
            try:
                spec = await asyncio.to_thread(self._command_spec, operation)
                if stop.is_set():
                    return self._cancelled_result(())
                result = await self._commands.execute(spec)
            except CommandCancelled as exc:
                return WorkspaceResult(
                    CommandOutcome(operation, exc.result),
                    ToolFailure(
                        "cancelled", "command cancelled after owned processes settled"
                    ),
                )
            except asyncio.CancelledError:
                # No command was started while the path admission awaited I/O.
                return self._cancelled_result(())
            except _FileFailure as exc:
                return WorkspaceResult(FileOutcome(()), exc.failure)
            error = None
            if result.timed_out:
                error = ToolFailure("timeout", "command exceeded its duration limit")
            elif result.exit_code != 0:
                error = ToolFailure(
                    "command_failed", f"command exited with status {result.exit_code}"
                )
            return WorkspaceResult(CommandOutcome(operation, result), error)
        worker = asyncio.create_task(
            asyncio.to_thread(self._file_batch, operation, stop)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            file_result = await self._settle(worker)
            return replace(
                file_result,
                error=ToolFailure(
                    "cancelled", "file operation cancelled after submitted I/O settled"
                ),
            )

    def _command_spec(self, operation: RunCommand) -> CommandSpec:
        return CommandSpec(
            operation.command,
            self._admit_path(operation.cwd, True),
            operation.timeout_s,
        )

    def _admit_path(self, value: str, directory: bool = False) -> Path:
        try:
            root = self.workspace.resolve(strict=True)
            raw = Path(value)
            path = (raw if raw.is_absolute() else root / raw).resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise _FileFailure(
                "path_resolution_error", f"workspace path could not be resolved: {exc}"
            ) from None
        if root != self.workspace:
            raise _FileFailure(
                "workspace_changed", "workspace must retain its admitted canonical path"
            )
        if not path.is_relative_to(root) or (not directory and path == root):
            raise _FileFailure(
                "path_outside_workspace",
                "path must resolve beneath the admitted workspace",
            )
        if directory:
            if not path.is_dir():
                raise _FileFailure(
                    "invalid_directory",
                    "command cwd must be an existing workspace directory",
                )
        elif not path.parent.is_dir():
            raise _FileFailure(
                "missing_parent", "file parent directory must already exist"
            )
        return path

    @staticmethod
    def _cancelled_result(receipts: tuple[FileReceipt, ...]) -> WorkspaceResult:
        return WorkspaceResult(
            FileOutcome(receipts),
            ToolFailure("cancelled", "operation cancelled before its next effect"),
        )

    def _file_batch(
        self, operation: CreateFiles | EditFiles, stop: threading.Event
    ) -> WorkspaceResult:
        receipts: list[FileReceipt] = []
        for item in operation.files:
            if stop.is_set():
                return self._cancelled_result(tuple(receipts))
            try:
                path = self._admit_path(item.path)
                before: bytes | None = None
                if isinstance(item, NewFile):
                    if path.exists():
                        raise _FileFailure(
                            "file_exists",
                            "file creation cannot overwrite an existing path",
                        )
                    after = item.content.encode("utf-8")
                else:
                    if not path.is_file():
                        raise _FileFailure(
                            "missing_file",
                            "edit target must be an existing regular file",
                        )
                    if path.stat().st_size > MAX_FILE_BYTES:
                        raise _FileFailure(
                            "file_too_large",
                            f"edit target exceeds {MAX_FILE_BYTES} bytes",
                        )
                    before = path.read_bytes()
                    if len(before) > MAX_FILE_BYTES:
                        raise _FileFailure(
                            "file_too_large",
                            f"edit target exceeds {MAX_FILE_BYTES} bytes",
                        )
                    try:
                        text = before.decode("utf-8")
                    except UnicodeDecodeError:
                        raise _FileFailure(
                            "invalid_encoding", "edit target must contain UTF-8 text"
                        ) from None
                    for edit in item.edits:
                        first_match = text.find(edit.old_text)
                        if first_match < 0 or first_match != text.rfind(edit.old_text):
                            raise _FileFailure(
                                "context_mismatch",
                                "old_text must occur exactly once in the current file",
                            )
                        text = text.replace(edit.old_text, edit.new_text, 1)
                    after = text.encode("utf-8")
                    if len(after) > MAX_FILE_BYTES:
                        raise _FileFailure(
                            "file_too_large",
                            f"edited content exceeds {MAX_FILE_BYTES} bytes",
                        )
                if stop.is_set():
                    return self._cancelled_result(tuple(receipts))
                receipt = FileReceipt(
                    _action(operation),
                    str(path),
                    hashlib.sha256(before).hexdigest() if before is not None else None,
                    hashlib.sha256(after).hexdigest(),
                )
                try:
                    self._publish(path, before, after, stop)
                except _PublishedFileCleanupError as exc:
                    receipts.append(receipt)
                    return WorkspaceResult(FileOutcome(tuple(receipts)), exc.failure)
                receipts.append(receipt)
            except _FileFailure as exc:
                return WorkspaceResult(FileOutcome(tuple(receipts)), exc.failure)
            except OSError as exc:
                return WorkspaceResult(
                    FileOutcome(tuple(receipts)),
                    ToolFailure(
                        "filesystem_error", f"filesystem operation failed: {exc}"
                    ),
                )
        return WorkspaceResult(FileOutcome(tuple(receipts)))

    @staticmethod
    def _publish(
        path: Path, before: bytes | None, after: bytes, stop: threading.Event
    ) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".meadow-bridge-", dir=path.parent)
        staged = Path(temporary)
        published = False
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            if stop.is_set():
                raise _FileFailure(
                    "cancelled", "operation cancelled before file publication"
                )
            if before is None:
                try:
                    # Atomic no-overwrite publication; never replace an intervening file.
                    os.link(staged, path)
                except FileExistsError:
                    raise _FileFailure(
                        "file_exists", "file appeared before creation could publish"
                    ) from None
            else:
                if path.read_bytes() != before:
                    raise _FileFailure(
                        "context_changed", "file changed before edit publication"
                    )
                staged.chmod(stat.S_IMODE(path.stat().st_mode))
                if stop.is_set():
                    raise _FileFailure(
                        "cancelled", "operation cancelled before file publication"
                    )
                os.replace(staged, path)
            published = True
        finally:
            try:
                staged.unlink(missing_ok=True)
            except OSError as exc:
                if published:
                    raise _PublishedFileCleanupError(
                        "staging_cleanup_failed",
                        f"file committed but staging cleanup failed for {staged}: {exc}",
                    ) from exc
                raise
