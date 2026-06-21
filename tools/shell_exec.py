"""
Shell execution tool — run shell commands with streaming and timeout handling.

Extracted from ``executor.py`` as part of the planned split
(target ≤400 lines per module).  Provides the async shell command
executor with real-time streaming, safety checks, and pipe cleanup.

Requires the host class (``ToolExecutor``) to provide:
- ``self.config`` — AgentConfig (blocked_commands, allowed_commands)
- ``self.workspace`` — resolved Path
- ``self._stream_cb`` — Callable | None (real-time chunk callback)
"""

import asyncio
import logging
import shlex

from .result import ToolResult

logger = logging.getLogger(__name__)


class ShellExecMixin:
    """Async shell command execution with streaming and timeout."""

    async def _tool_run_shell(
        self, command: str, timeout: int = 120
    ) -> ToolResult:
        """Execute a shell command."""
        # Safety checks
        cmd_lower = command.lower().strip()

        # Check blocked patterns (hard block — unforgivable operations)
        for blocked in self.config.blocked_commands:
            if blocked.lower() in cmd_lower:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Blocked command pattern detected: {blocked}",
                )

        # Check if first word is allowed (soft warning — safety_guard is the real gate)
        first_word = command.strip().split()[0] if command.strip() else ""
        if first_word and first_word not in self.config.allowed_commands:
            logger.debug("Command '%s' not in allowed_commands whitelist, proceeding", first_word)

        # Pre-validate command tokenization — catches unterminated quotes,
        # mismatched escapes, and other shell injection indicators before
        # the command reaches the shell interpreter.
        try:
            tokens = shlex.split(command)
        except ValueError as e:
            logger.warning("Shell command failed shlex tokenization: %s — command: %.200s", e, command)
            return ToolResult(
                success=False,
                output="",
                error=f"Command parsing error (possible injection): {e}",
            )
        if not tokens:
            return ToolResult(
                success=False,
                output="",
                error="Empty command after tokenization.",
            )
        logger.debug("Shell command tokenized: %s", tokens[:10])  # log first 10 tokens max

        MAX_OUTPUT_BYTES = 500_000  # cap total stdout+stderr to prevent memory exhaustion
        proc = None
        try:
            # DEVNULL on stdin prevents hangs when child processes try to read
            # from inherited stdin (common cause of "exec task freeze").
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
            )

            # Stream stdout+stderr concurrently — avoids pipe-buffer deadlock
            # and hangs from child processes inheriting pipes.
            # When a stream_callback is set, emit chunks in real-time so the
            # user sees command progress instead of waiting for completion.
            async def _read_stream(stream, chunks: list[bytes]):
                total = 0
                while True:
                    try:
                        chunk = await stream.read(65536)
                    except (asyncio.CancelledError, Exception):
                        return
                    if not chunk:
                        return
                    total += len(chunk)
                    if total <= MAX_OUTPUT_BYTES:
                        chunks.append(chunk)
                    # Real-time streaming to UI
                    if self._stream_cb:
                        try:
                            text = chunk.decode("utf-8", errors="replace")
                            self._stream_cb("run_shell", text)
                        except Exception:
                            pass

            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            stdout_task = asyncio.create_task(_read_stream(proc.stdout, stdout_chunks))
            stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_chunks))

            # Wait for process to exit (with timeout), NOT for pipes to close
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                # Kill the process FIRST — sends EOF to pipes, unblocking reader tasks
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                # Cancel reader tasks AFTER kill (kill unblocks pipe reads)
                stdout_task.cancel()
                stderr_task.cancel()
                try:
                    await proc.wait()
                except (ProcessLookupError, Exception):
                    pass
                return ToolResult(
                    success=False, output="",
                    error=f"Command timed out after {timeout}s",
                )

            # Process exited — wait briefly for remaining pipe data
            try:
                await asyncio.wait_for(stdout_task, timeout=5)
            except asyncio.TimeoutError:
                stdout_task.cancel()
            try:
                await asyncio.wait_for(stderr_task, timeout=5)
            except asyncio.TimeoutError:
                stderr_task.cancel()

            stdout_bytes = b"".join(stdout_chunks)
            stderr_bytes = b"".join(stderr_chunks)
            output = stdout_bytes.decode("utf-8", errors="replace")
            if stderr_bytes:
                output += f"\n[stderr]\n{stderr_bytes.decode('utf-8', errors='replace')}"
            returncode = proc.returncode if proc.returncode is not None else -1
            if returncode != 0:
                output += f"\n[exit code: {returncode}]"
            if len(stdout_bytes) + len(stderr_bytes) >= MAX_OUTPUT_BYTES:
                output += "\n[output truncated — exceeded 500KB limit]"
            return ToolResult(
                success=returncode == 0,
                output=output.strip() or "(no output)",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="", error=f"Command failed: {e}"
            )
        finally:
            # Explicitly close pipes to prevent "I/O operation on closed pipe"
            # during BaseSubprocessTransport.__del__ at GC time.
            if proc is not None:
                for pipe in (proc.stdin, proc.stdout, proc.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except Exception:
                            pass
