"""Persistent PowerShell/bash sessions for interactive terminal in the API server."""
import logging
import os
import queue
import subprocess
import threading
import uuid

logger = logging.getLogger(__name__)

_shell_sessions: dict[str, tuple[subprocess.Popen, "queue.Queue[str]", threading.Lock, str, str]] = {}
_shell_lock = threading.Lock()
_restarting: set[str] = set()  # sids currently being restarted — prevents duplicate shells


def shell_open(cwd: str, sid: str = "", token_hash: str = "") -> str:
    """Start a persistent shell process. Returns session ID.

    Uses PowerShell on Windows, bash on Linux/macOS.

    Args:
        cwd: Working directory for the shell.
        sid: Optional session ID (auto-generated if empty).
        token_hash: Auth token hash — binds this session to the caller.
    """
    sid = sid or uuid.uuid4().hex[:10]
    out_queue: "queue.Queue[str]" = queue.Queue()

    if os.name == "nt":
        # Windows: PowerShell with binary pipes to completely bypass
        # the internal _readerthread (which ignores encoding and uses GBK).
        # We create our own reader threads that decode UTF-8 manually.
        proc = subprocess.Popen(
            ["powershell.exe", "-NoLogo", "-NoExit"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd,
        )
        # Switch PS to UTF-8 output (must use bytes since stdin is binary)
        proc.stdin.write("chcp 65001 >$null\n".encode("utf-8"))
        proc.stdin.write("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n".encode("utf-8"))
        proc.stdin.flush()
        prompt_text = "PS> "
    else:
        # Linux/macOS: bash with binary pipes
        proc = subprocess.Popen(
            ["bash", "--norc"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=cwd,
        )
        prompt_text = "$ "

    def _reader(pipe, label):
        """Read binary lines from a pipe, decode UTF-8, and enqueue."""
        for line_bytes in pipe:
            try:
                out_queue.put(line_bytes.decode("utf-8", errors="replace"))
            except Exception:
                pass

    threading.Thread(target=_reader, args=(proc.stdout, "stdout"), daemon=True).start()
    threading.Thread(target=_reader, args=(proc.stderr, "stderr"), daemon=True).start()

    # Drain startup banner — wait for initial output then drain
    try:
        out_queue.get(timeout=3.0)  # first line proves the process is alive
    except queue.Empty:
        pass  # process produced no banner, proceed anyway
    while not out_queue.empty():
        try:
            out_queue.get_nowait()
        except queue.Empty:
            break

    with _shell_lock:
        _shell_sessions[sid] = (proc, out_queue, threading.Lock(), prompt_text, token_hash)
    logger.info("Shell opened: %s", sid)
    return sid


def shell_ensure(sid: str, cwd: str, token_hash: str = ""):
    """Get or create a shell session. Auto-restarts dead processes.

    Validates that the caller's token_hash matches the session's stored hash.
    Returns (None, None, None, "", "token_mismatch") on auth failure.

    Race-condition safe: uses ``_restarting`` set so only one thread
    restarts a given session — concurrent callers either get the live
    process or wait for the restart to finish.
    """
    import time as _time

    with _shell_lock:
        # Another thread is restarting this session — wait for it
        if sid in _restarting:
            pass  # fall through to retry loop outside the lock

        elif sid in _shell_sessions:
            entry = _shell_sessions[sid]
            proc, outq, lock, prompt, stored_token = entry
            # Verify the caller owns this session
            if stored_token and token_hash and stored_token != token_hash:
                logger.warning(
                    "Shell session %s token mismatch: caller=%s... != stored=%s...",
                    sid[:6], token_hash[:12], stored_token[:12],
                )
                return (None, None, None, "", "token_mismatch")
            if proc.poll() is not None:
                # Dead process — mark restarting so no other thread
                # duplicates the restart.  Remove the old entry first
                # so a concurrent shell_ensure on a different thread
                # doesn't find the zombie.
                logger.warning(
                    "Shell process %s died (rc=%d), restarting",
                    sid[:6], proc.returncode,
                )
                _restarting.add(sid)
                del _shell_sessions[sid]
            else:
                # Live process, good to go
                return (proc, outq, lock, prompt, "")

    # Retry loop: if another thread is restarting, spin briefly then
    # re-check.  In practice this loop exits on the first re-check
    # because shell_open completes in < 1s.
    for _ in range(20):
        with _shell_lock:
            if sid in _shell_sessions:
                entry = _shell_sessions[sid]
                return (*entry, "")
        if sid not in _restarting:
            break  # restart done (or never started) — create below
        _time.sleep(0.05)

    # Create (or recreate) the shell.  shell_open overwrites on
    # duplicate sid so the last write always wins.
    shell_open(cwd, sid=sid, token_hash=token_hash)
    with _shell_lock:
        _restarting.discard(sid)
        entry = _shell_sessions.get(sid)
        if entry:
            return (*entry, "")
        return (None, None, None, "", "")


def shell_close(sid: str):
    """Close a single shell session, terminating the process and releasing pipes."""
    with _shell_lock:
        entry = _shell_sessions.pop(sid, None)
    if entry:
        proc, _, _, _ = entry
        try:
            proc.stdin.write(b"exit\n")
            proc.stdin.flush()
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        # Close remaining pipes to prevent Windows proactor "unclosed transport" warnings
        if proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:
                pass
        logger.info("Shell closed: %s", sid)


def shell_close_all():
    """Close all active shell sessions. Call during server shutdown."""
    with _shell_lock:
        sids = list(_shell_sessions.keys())
    for sid in sids:
        shell_close(sid)


def get_shell_sessions():
    """Return the module-level shell sessions dict (for use by handler)."""
    return _shell_sessions
