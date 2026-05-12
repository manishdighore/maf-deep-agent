"""FilesystemProvider — exposes virtual filesystem tools scoped to the session.

The LLM sees ``ls``, ``read_file``, ``write_file``, ``edit_file``, ``glob``,
and ``grep`` as regular tools.  The underlying ``ThreadedStateFilesystem``
uses ``session.session_id`` as the thread key — the agent never touches it.

Usage::

    from deep_agent.services.filesystem import ThreadedStateFilesystem
    from deep_agent.providers import FilesystemProvider

    fs = ThreadedStateFilesystem()
    provider = FilesystemProvider(fs=fs)
"""

from __future__ import annotations

from typing import Any

from agent_framework import FunctionTool, Message
from agent_framework._sessions import AgentSession, ContextProvider, SessionContext

from deep_agent.services.filesystem import ThreadedStateFilesystem


class FilesystemProvider(ContextProvider):
    """Injects session-scoped filesystem tools into every agent turn.

    Parameters
    ----------
    fs : ThreadedStateFilesystem
        The shared (process-wide) filesystem instance.
    inject_summary : bool
        If ``True``, inject a system message listing current files
        at the start of each turn so the LLM is aware of the workspace.
    source_id : str
        Provider source identifier (default ``"filesystem"``).
    """

    def __init__(
        self,
        fs: ThreadedStateFilesystem,
        *,
        inject_summary: bool = True,
        source_id: str = "filesystem",
    ) -> None:
        super().__init__(source_id)
        self._fs = fs
        self._inject_summary = inject_summary

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        tid = session.session_id

        context.extend_tools(
            self.source_id,
            self._build_tools(tid),
        )

        if self._inject_summary:
            summary = self._workspace_summary(tid)
            if summary:
                context.extend_messages(
                    self.source_id,
                    [Message("system", [summary])],
                )

    def _build_tools(self, tid: str) -> list[FunctionTool]:
        """Create the 6 filesystem FunctionTools bound to *tid*."""
        fs = self._fs

        def ls(path: str = "/") -> str:
            """List files and directories at the given path.

            Args:
                path: Absolute virtual path to list (default "/").

            Returns:
                Newline-separated listing of entries.
            """
            result = fs.ls(tid, path)
            if result.error:
                return f"Error: {result.error}"
            if not result.entries:
                return "(empty directory)"
            lines: list[str] = []
            for entry in result.entries:
                p = entry.get("path", "")
                if entry.get("is_dir"):
                    lines.append(f"  {p}")
                else:
                    size = entry.get("size", 0)
                    lines.append(f"  {p}  ({size} chars)")
            return "\n".join(lines)

        def read_file(
            file_path: str,
            offset: int = 0,
            limit: int = 500,
        ) -> str:
            """Read a file from the workspace.

            Args:
                file_path: Absolute path to the file to read.
                offset: Line offset to start reading from (0-indexed).
                limit: Maximum number of lines to return.

            Returns:
                Line-numbered file content.
            """
            result = fs.read(tid, file_path, offset=offset, limit=limit)
            if result.error:
                return f"Error: {result.error}"
            header = f"({result.total_lines} lines total)"
            if result.offset and result.offset > 0:
                header += f" showing from line {result.offset + 1}"
            return f"{header}\n{result.content}"

        def write_file(file_path: str, content: str) -> str:
            """Create a new file in the workspace. Fails if file already exists — use edit_file to modify.

            Args:
                file_path: Absolute path for the new file.
                content: Full file content to write.

            Returns:
                Confirmation or error message.
            """
            result = fs.write(tid, file_path, content)
            if result.error:
                return f"Error: {result.error}"
            return f"Created {result.path} ({len(content)} chars)"

        def edit_file(
            file_path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> str:
            """Edit a file by replacing an exact string match.

            Args:
                file_path: Absolute path to the file to edit.
                old_string: Exact string to find (must match including whitespace).
                new_string: Replacement string.
                replace_all: If True, replace all occurrences.

            Returns:
                Confirmation with number of replacements, or error message.
            """
            result = fs.edit(
                tid, file_path, old_string, new_string,
                replace_all=replace_all,
            )
            if result.error:
                return f"Error: {result.error}"
            return f"Edited {result.path} ({result.occurrences} replacement(s))"

        def glob(pattern: str, path: str = "/") -> str:
            """Find files matching a glob pattern (supports *, **, ?).

            Args:
                pattern: Glob pattern (e.g. "**/*.py", "*.md").
                path: Base directory to search from (default "/").

            Returns:
                Newline-separated list of matching file paths.
            """
            result = fs.glob(tid, pattern, path)
            if result.error:
                return f"Error: {result.error}"
            if not result.matches:
                return "No files matched."
            return "\n".join(m.get("path", "") for m in result.matches)

        def grep(
            pattern: str,
            path: str = "/",
            file_glob: str | None = None,
        ) -> str:
            """Search file contents for a text pattern.

            Args:
                pattern: Literal text to search for.
                path: Directory to search in (default "/").
                file_glob: Optional filename filter (e.g. "*.py").

            Returns:
                Matching lines grouped by file, or "No matches found."
            """
            result = fs.grep(tid, pattern, path, file_glob=file_glob)
            if result.error:
                return f"Error: {result.error}"
            if not result.matches:
                return "No matches found."
            lines: list[str] = []
            current_file = ""
            for m in result.matches:
                if m["path"] != current_file:
                    current_file = m["path"]
                    lines.append(f"\n{current_file}:")
                lines.append(f"  {m['line']}: {m['text']}")
            return "\n".join(lines).strip()

        return [
            FunctionTool(func=ls, name="ls", description="List files and directories at a path in the workspace."),
            FunctionTool(func=read_file, name="read_file", description="Read a file from the workspace with line numbers and pagination."),
            FunctionTool(func=write_file, name="write_file", description="Create a new file in the workspace (fails if file exists)."),
            FunctionTool(func=edit_file, name="edit_file", description="Edit a file by replacing an exact string match."),
            FunctionTool(func=glob, name="glob", description="Find files matching a glob pattern (*, **, ?)."),
            FunctionTool(func=grep, name="grep", description="Search file contents for a text pattern."),
        ]

    def _workspace_summary(self, tid: str) -> str | None:
        """Build a short overview of the workspace for context injection."""
        count = self._fs.file_count(tid)
        if count == 0:
            return None

        result = self._fs.ls(tid, "/")
        if result.error or not result.entries:
            return None

        lines = [f"[Workspace: {count} file(s)]"]
        for entry in result.entries[:20]:
            p = entry.get("path", "")
            if entry.get("is_dir"):
                lines.append(f"  📁 {p}")
            else:
                lines.append(f"  📄 {p}")
        if count > 20:
            lines.append(f"  ... and {count - 20} more")
        return "\n".join(lines)
