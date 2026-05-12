"""Thread-segmented in-memory filesystem with async blob persistence.

Inspired by DeepAgents' StateBackend, this provides a lightweight virtual
filesystem that keeps files in a ``dict[str, FileData]`` per thread.

Key design goals:
  - **Thread isolation** — each conversation / session gets its own namespace
    so agents sharing the same process never collide.
  - **Blob persistence** — a thread's entire file tree can be serialised to
    Azure Blob Storage (or any ``BlobClient`` duck-type) and restored later,
    enabling durable agent memory without a real filesystem.
  - **Non-blocking I/O** — blob save/load run in ``asyncio.to_thread`` so they
    never block the event loop.
  - **Zero external deps for core ops** — ``ls``, ``read``, ``write``, ``edit``,
    ``glob``, ``grep`` operate on the in-memory dict and are pure Python.

Usage::

    from deep_agent.services.filesystem import ThreadedStateFilesystem

    fs = ThreadedStateFilesystem()

    # write / read
    fs.write("thread-1", "/notes/todo.md", "- buy milk")
    result = fs.read("thread-1", "/notes/todo.md")

    # persist to blob
    await fs.save_to_blob("thread-1", blob_client)

    # restore later
    await fs.load_from_blob("thread-1", blob_client)
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────


class FileData(TypedDict, total=False):
    """In-memory representation of a single file."""

    content: str
    encoding: str
    created_at: str
    modified_at: str


class FileInfo(TypedDict, total=False):
    """Entry returned by ``ls`` / ``glob``."""

    path: str
    is_dir: bool
    size: int
    modified_at: str


class GrepMatch(TypedDict):
    """Single hit from ``grep``."""

    path: str
    line: int
    text: str


@dataclass
class ReadResult:
    content: str | None = None
    total_lines: int | None = None
    offset: int | None = None
    limit: int | None = None
    error: str | None = None


@dataclass
class WriteResult:
    path: str | None = None
    error: str | None = None


@dataclass
class EditResult:
    path: str | None = None
    occurrences: int | None = None
    error: str | None = None


@dataclass
class LsResult:
    entries: list[FileInfo] = field(default_factory=list)
    error: str | None = None


@dataclass
class GlobResult:
    matches: list[FileInfo] = field(default_factory=list)
    error: str | None = None


@dataclass
class GrepResult:
    matches: list[GrepMatch] = field(default_factory=list)
    error: str | None = None


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_path(path: str) -> str:
    """Normalise and validate a virtual path."""
    if not path:
        raise ValueError("Path must not be empty")
    if not path.startswith("/"):
        path = "/" + path
    while "//" in path:
        path = path.replace("//", "/")
    if ".." in path:
        raise ValueError("Path traversal ('..') is not allowed")
    return path


def _create_file_data(content: str, encoding: str = "utf-8") -> FileData:
    now = _now_iso()
    return FileData(
        content=content,
        encoding=encoding,
        created_at=now,
        modified_at=now,
    )


def _update_file_data(existing: FileData, content: str) -> FileData:
    return FileData(
        content=content,
        encoding=existing.get("encoding", "utf-8"),
        created_at=existing.get("created_at", _now_iso()),
        modified_at=_now_iso(),
    )


def _glob_matches(path: str, pattern: str) -> bool:
    """Match *path* against a glob *pattern* with ``**`` support."""
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*\*", "§GLOBSTAR§")
    escaped = escaped.replace(r"\*", r"[^/]*")
    escaped = escaped.replace(r"\?", r"[^/]")
    escaped = escaped.replace("§GLOBSTAR§", ".*")
    return bool(re.fullmatch(escaped, path))


# ──────────────────────────────────────────────────────────────────────
# ThreadedStateFilesystem
# ──────────────────────────────────────────────────────────────────────


class ThreadedStateFilesystem:
    """In-memory virtual filesystem with per-thread segmentation.

    Each *thread_id* (typically the conversation / session id) has its own
    isolated ``dict[str, FileData]``.  All paths are virtual and absolute
    (``/notes/todo.md``).

    Thread safety: a per-thread ``threading.Lock`` is acquired for every
    mutation so the object is safe to share across asyncio tasks and OS
    threads.

    Parameters
    ----------
    max_file_size : int
        Reject ``write``/``edit`` calls whose resulting content exceeds
        this many characters (default 5 MB worth of chars).
    """

    def __init__(self, *, max_file_size: int = 5_000_000) -> None:
        self._stores: dict[str, dict[str, FileData]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._max_file_size = max_file_size

    def _lock_for(self, thread_id: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._locks.get(thread_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[thread_id] = lock
            return lock

    def _files(self, thread_id: str) -> dict[str, FileData]:
        with self._meta_lock:
            store = self._stores.get(thread_id)
            if store is None:
                store = {}
                self._stores[thread_id] = store
            return store

    # ── ls ──────────────────────────────────────────────────────────

    def ls(self, thread_id: str, path: str = "/") -> LsResult:
        """List files and immediate subdirectories under *path*."""
        try:
            path = _validate_path(path)
        except ValueError as exc:
            return LsResult(error=str(exc))

        normalized = path if path.endswith("/") else path + "/"
        files = self._files(thread_id)

        entries: list[FileInfo] = []
        subdirs: set[str] = set()

        with self._lock_for(thread_id):
            for key, fd in files.items():
                if not key.startswith(normalized):
                    continue
                relative = key[len(normalized):]
                if "/" in relative:
                    subdir_name = relative.split("/")[0]
                    subdirs.add(normalized + subdir_name + "/")
                else:
                    entries.append(FileInfo(
                        path=key,
                        is_dir=False,
                        size=len(fd.get("content", "")),
                        modified_at=fd.get("modified_at", ""),
                    ))

        for sd in sorted(subdirs):
            entries.append(FileInfo(path=sd, is_dir=True, size=0, modified_at=""))

        entries.sort(key=lambda e: e.get("path", ""))
        return LsResult(entries=entries)

    # ── read ────────────────────────────────────────────────────────

    def read(
        self,
        thread_id: str,
        file_path: str,
        *,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """Read *limit* lines starting at *offset* (0-indexed)."""
        try:
            file_path = _validate_path(file_path)
        except ValueError as exc:
            return ReadResult(error=str(exc))

        files = self._files(thread_id)

        with self._lock_for(thread_id):
            fd = files.get(file_path)
            if fd is None:
                return ReadResult(error=f"File not found: {file_path}")

            raw = fd.get("content", "")
            lines = raw.splitlines(keepends=True)
            total = len(lines)

            if offset >= total:
                return ReadResult(
                    error=f"Offset {offset} exceeds file length ({total} lines)",
                )

            end = min(offset + limit, total)
            window = lines[offset:end]

        numbered: list[str] = []
        width = len(str(end))
        for i, line in enumerate(window, start=offset + 1):
            numbered.append(f"{i:>{width}} | {line.rstrip()}")

        return ReadResult(
            content="\n".join(numbered),
            total_lines=total,
            offset=offset,
            limit=limit,
        )

    # ── write ───────────────────────────────────────────────────────

    def write(
        self,
        thread_id: str,
        file_path: str,
        content: str,
        *,
        overwrite: bool = False,
    ) -> WriteResult:
        """Create a new file.  Fails if already exists unless *overwrite*."""
        try:
            file_path = _validate_path(file_path)
        except ValueError as exc:
            return WriteResult(error=str(exc))

        if len(content) > self._max_file_size:
            return WriteResult(
                error=f"Content exceeds max size ({self._max_file_size} chars)",
            )

        files = self._files(thread_id)

        with self._lock_for(thread_id):
            if file_path in files and not overwrite:
                return WriteResult(
                    error=(
                        f"File already exists: {file_path}.  "
                        "Use edit() to modify, or pass overwrite=True."
                    ),
                )
            files[file_path] = _create_file_data(content)

        return WriteResult(path=file_path)

    # ── edit ────────────────────────────────────────────────────────

    def edit(
        self,
        thread_id: str,
        file_path: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> EditResult:
        """Replace exact occurrences of *old_string* with *new_string*."""
        try:
            file_path = _validate_path(file_path)
        except ValueError as exc:
            return EditResult(error=str(exc))

        files = self._files(thread_id)

        with self._lock_for(thread_id):
            fd = files.get(file_path)
            if fd is None:
                return EditResult(error=f"File not found: {file_path}")

            content = fd.get("content", "")
            count = content.count(old_string)

            if count == 0:
                return EditResult(error=f"String not found in {file_path}")
            if count > 1 and not replace_all:
                return EditResult(
                    error=(
                        f"'{old_string}' appears {count} times.  "
                        "Pass replace_all=True or use a more specific string."
                    ),
                )

            new_content = content.replace(old_string, new_string)

            if len(new_content) > self._max_file_size:
                return EditResult(
                    error=f"Resulting content would exceed max size ({self._max_file_size} chars)",
                )

            files[file_path] = _update_file_data(fd, new_content)

        return EditResult(path=file_path, occurrences=count)

    # ── glob ────────────────────────────────────────────────────────

    def glob(
        self,
        thread_id: str,
        pattern: str,
        path: str = "/",
    ) -> GlobResult:
        """Find files matching a fnmatch/glob pattern."""
        try:
            path = _validate_path(path)
        except ValueError as exc:
            return GlobResult(error=str(exc))

        normalized = path if path.endswith("/") else path + "/"
        files = self._files(thread_id)

        full_pattern = normalized + pattern.lstrip("/")

        infos: list[FileInfo] = []
        with self._lock_for(thread_id):
            for key, fd in files.items():
                if not key.startswith(normalized):
                    continue
                if _glob_matches(key, full_pattern):
                    infos.append(FileInfo(
                        path=key,
                        is_dir=False,
                        size=len(fd.get("content", "")),
                        modified_at=fd.get("modified_at", ""),
                    ))

        infos.sort(key=lambda e: e.get("path", ""))
        return GlobResult(matches=infos)

    # ── grep ────────────────────────────────────────────────────────

    def grep(
        self,
        thread_id: str,
        pattern: str,
        path: str | None = None,
        *,
        file_glob: str | None = None,
        is_regex: bool = False,
    ) -> GrepResult:
        """Search file contents for *pattern*."""
        path = path or "/"
        try:
            path = _validate_path(path)
        except ValueError as exc:
            return GrepResult(error=str(exc))

        normalized = path if path.endswith("/") else path + "/"

        if is_regex:
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                return GrepResult(error=f"Invalid regex: {exc}")
            test = rx.search
        else:
            def test(line: str) -> bool:  # type: ignore[assignment]
                return pattern in line

        files = self._files(thread_id)
        matches: list[GrepMatch] = []

        with self._lock_for(thread_id):
            for key, fd in files.items():
                if not key.startswith(normalized):
                    continue
                if file_glob and not fnmatch.fnmatch(key.rsplit("/", 1)[-1], file_glob):
                    continue
                content = fd.get("content", "")
                for line_num, line in enumerate(content.split("\n"), 1):
                    if test(line):
                        matches.append(GrepMatch(
                            path=key,
                            line=line_num,
                            text=line,
                        ))

        return GrepResult(matches=matches)

    # ── delete ──────────────────────────────────────────────────────

    def delete(self, thread_id: str, file_path: str) -> WriteResult:
        """Remove a single file."""
        try:
            file_path = _validate_path(file_path)
        except ValueError as exc:
            return WriteResult(error=str(exc))

        files = self._files(thread_id)
        with self._lock_for(thread_id):
            if file_path not in files:
                return WriteResult(error=f"File not found: {file_path}")
            del files[file_path]

        return WriteResult(path=file_path)

    # ── thread management ───────────────────────────────────────────

    def list_threads(self) -> list[str]:
        with self._meta_lock:
            return [tid for tid, store in self._stores.items() if store]

    def clear_thread(self, thread_id: str) -> int:
        files = self._files(thread_id)
        with self._lock_for(thread_id):
            count = len(files)
            files.clear()
        return count

    def file_count(self, thread_id: str) -> int:
        return len(self._files(thread_id))

    # ── blob persistence ────────────────────────────────────────────

    async def save_to_blob(
        self,
        thread_id: str,
        blob_client: Any,
        *,
        pretty: bool = False,
    ) -> int:
        """Serialise *thread_id*'s files to a blob as JSON."""
        files = self._files(thread_id)

        with self._lock_for(thread_id):
            snapshot = dict(files)

        payload = json.dumps(snapshot, indent=2 if pretty else None).encode("utf-8")
        await asyncio.to_thread(blob_client.upload_blob, payload, overwrite=True)
        logger.info("Saved %d files for thread %s (%d bytes)", len(snapshot), thread_id, len(payload))
        return len(snapshot)

    async def load_from_blob(
        self,
        thread_id: str,
        blob_client: Any,
        *,
        merge: bool = False,
    ) -> int:
        """Load files from a blob into thread *thread_id*."""

        def _download() -> bytes:
            stream = blob_client.download_blob()
            if hasattr(stream, "readall"):
                return stream.readall()
            return stream.read()

        raw = await asyncio.to_thread(_download)
        data: dict[str, FileData] = json.loads(raw)

        files = self._files(thread_id)
        with self._lock_for(thread_id):
            if merge:
                files.update(data)
            else:
                files.clear()
                files.update(data)

        logger.info("Loaded %d files for thread %s (%d bytes)", len(data), thread_id, len(raw))
        return len(data)

    # ── snapshot / restore ──────────────────────────────────────────

    def snapshot(self, thread_id: str) -> dict[str, FileData]:
        files = self._files(thread_id)
        with self._lock_for(thread_id):
            return {k: dict(v) for k, v in files.items()}  # type: ignore[misc]

    def restore(self, thread_id: str, data: dict[str, FileData]) -> int:
        files = self._files(thread_id)
        with self._lock_for(thread_id):
            files.clear()
            files.update(data)
        return len(data)
