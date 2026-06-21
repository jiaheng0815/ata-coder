"""Thread-safe session store for the API server."""
import asyncio
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from .config import AppConfig
from .tools import ToolExecutor
from .agent import CoderAgent
from .agent_subsystems import AgentSubsystems
from .permissions import PermissionStore, PermissionMode

logger = logging.getLogger(__name__)


class SessionStore:
    """Thread-safe session store for the API server."""

    # Auto-cleanup: sessions idle for this many seconds are evicted.
    SESSION_TTL_SECONDS = 1800  # 30 minutes

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, CoderAgent] = {}
        self._metadata: dict[str, dict] = {}
        # Per-token session isolation: token_hash → set of session IDs
        self._token_sessions: dict[str, set[str]] = {}

    @staticmethod
    def _hash_token(token: str) -> str:
        """Hash a token for session isolation lookup."""
        import hashlib
        return hashlib.sha256(token.encode()).hexdigest()[:16]

    def _shutdown_agent(self, agent: "CoderAgent") -> None:
        """Shutdown an agent safely, handling threaded vs async contexts."""
        try:
            # Try to get the running event loop first
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — safe to use asyncio.run()
            try:
                asyncio.run(agent.shutdown())
            except Exception:
                logger.exception("Agent shutdown via asyncio.run() failed")
            return
        # Event loop is running — schedule on it via thread-safe mechanism
        try:
            future = asyncio.run_coroutine_threadsafe(agent.shutdown(), loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        except Exception:
            logger.debug("Could not schedule agent shutdown on running loop — skipping")

    def _evict_expired(self):
        """Remove sessions that have been idle longer than SESSION_TTL_SECONDS."""
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for sid, meta in self._metadata.items():
                created_str = meta.get("created", "")
                last_active_str = meta.get("last_active", created_str)
                try:
                    t = datetime.strptime(last_active_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, OverflowError):
                    t = 0  # unparseable — evict (conservative: assume very old)
                if now - t > self.SESSION_TTL_SECONDS:
                    expired.append(sid)
            # Collect agents to shut down — do expensive _shutdown_agent OUTSIDE the lock
            agents_to_shutdown: list = []
            for sid in expired:
                agent = self._sessions.pop(sid, None)
                if agent:
                    agents_to_shutdown.append(agent)
                meta = self._metadata.pop(sid, None)
                # Clean up token isolation mapping
                if meta and "token_hash" in meta:
                    th = meta["token_hash"]
                    if th in self._token_sessions:
                        self._token_sessions[th].discard(sid)
                        if not self._token_sessions[th]:
                            del self._token_sessions[th]
        # Shut down outside the lock to avoid blocking all session operations
        for agent in agents_to_shutdown:
            self._shutdown_agent(agent)
        if expired:
            logger.info("Evicted %d expired session(s)", len(expired))

    def create(self, config: AppConfig, skill: str = "",
               token_hash: str = "") -> tuple[str, CoderAgent]:
        # Use full 32-char UUID for session IDs (was 12-char, brute-forceable)
        sid = uuid.uuid4().hex

        # Prune expired sessions before creating a new one
        self._evict_expired()

        # Guard: if a session with this ID already exists, clean it up first
        with self._lock:
            existing = self._sessions.pop(sid, None)
            old_meta = self._metadata.pop(sid, None)
            if old_meta and "token_hash" in old_meta:
                th = old_meta["token_hash"]
                if th in self._token_sessions:
                    self._token_sessions[th].discard(sid)
        if existing:
            self._shutdown_agent(existing)

        tool_exec = ToolExecutor(config.agent)

        # Build AgentSubsystems container (replaces loose kwargs)
        perms = PermissionStore(config.agent.workspace_dir)

        # API mode: permission model
        # By default, require explicit allow-all env var or auth token.
        # Without either, shell/write operations are DENIED for safety.
        if os.environ.get("ATA_CODER_ALLOW_ALL", "").lower() in ("1", "true", "yes"):
            perms.set_category_rule("shell", PermissionMode.ALLOW)
            perms.set_category_rule("write", PermissionMode.ALLOW)
            # Audit logger: records every allow-all decision for forensic trace
            audit_logger = logging.getLogger("ata_coder.audit.allow_all")
            audit_logger.setLevel(logging.WARNING)
            audit_logger.warning(
                "⚠️  ATA_CODER_ALLOW_ALL=1 — ALL shell & write operations "
                "will be silently allowed without permission prompts. "
                "This is intended for headless/automated environments only."
            )
            # Wrap the permission store to emit audit log entries for every
            # check that would have required confirmation in interactive mode.
            _orig_check = perms.check
            def _audited_check(tool_name: str, arguments: dict | None = None) -> bool:
                result = _orig_check(tool_name, arguments)
                from .permissions import tool_category
                category = tool_category(tool_name)
                if result and category in ("shell", "write"):
                    audit_logger.info(
                        "ALLOW_ALL: %s | %s | %s",
                        category, tool_name, str(arguments or {})[:200]
                    )
                # CRITICAL safety: hard-block destructive patterns even in allow-all mode.
                # Patterns like rm -rf /, mkfs, dd writes, fork bombs are never auto-allowed.
                if category == "shell" and result:
                    from .safety_guard import analyze_command, RiskLevel
                    cmd = str((arguments or {}).get("command", ""))
                    if cmd:
                        risk = analyze_command(cmd)
                        if risk.risk == RiskLevel.CRITICAL:
                            audit_logger.critical(
                                "ALLOW_ALL BLOCKED (CRITICAL): %s | %s",
                                tool_name, risk.description or cmd[:200]
                            )
                            return False
                return result
            perms.check = _audited_check
        elif os.environ.get("ATA_CODER_API_TOKEN", ""):
            # Token auth is configured — trust the caller's auth layer,
            # but still require explicit allow-all for write/shell.
            # Read tools are always allowed; write/shell default to DENY
            # unless the client sets per-call permissions.
            perms.set_prompt_callback(lambda n, a, c: False)  # deny by default
            logger.info(
                "API token configured — write/shell operations require "
                "explicit ATA_CODER_ALLOW_ALL=1 or per-call permission."
            )
        else:
            # No auth, no allow-all — DENY all write/shell for safety
            perms.set_category_rule("shell", PermissionMode.DENY)
            perms.set_category_rule("write", PermissionMode.DENY)
            logger.warning(
                "⚠️  No ATA_CODER_API_TOKEN set — write & shell operations "
                "are DENIED. Set ATA_CODER_API_TOKEN for auth, and "
                "ATA_CODER_ALLOW_ALL=1 to enable write/shell in API mode."
            )

        from .skills import get_skill_manager
        from .memory import get_memory_store
        from .project import ProjectDetector

        skill_mgr = get_skill_manager()
        if skill:
            skill_mgr.activate(skill)

        subsystems = AgentSubsystems(
            skills=skill_mgr,
            memory=get_memory_store(),
            permissions=perms,
            project_info=ProjectDetector(config.agent.workspace_dir).detect(),
        )

        agent = CoderAgent(
            config=config,
            tool_executor=tool_exec,
            subsystems=subsystems,
        )

        now_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._sessions[sid] = agent
            self._metadata[sid] = {
                "created": now_ts,
                "last_active": now_ts,
                "messages": 0,
                "tool_calls": 0,
                "skill": skill,
                "model": config.llm.model,
                "token_hash": token_hash,
            }
            if token_hash:
                self._token_sessions.setdefault(token_hash, set()).add(sid)
        return sid, agent

    def get(self, sid: str, token_hash: str = "") -> CoderAgent | None:
        self._evict_expired()
        with self._lock:
            agent = self._sessions.get(sid)
            if agent and sid in self._metadata:
                meta = self._metadata[sid]
                # Enforce per-token session isolation
                if meta.get("token_hash") and token_hash and meta["token_hash"] != token_hash:
                    return None
                self._metadata[sid]["last_active"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
            return agent

    def get_or_create(self, sid: str | None, config: AppConfig, skill: str = "",
                      token_hash: str = "") -> tuple[str, CoderAgent]:
        if sid:
            existing = self.get(sid, token_hash)
            if existing:
                return sid, existing
        return self.create(config, skill, token_hash)

    def update_meta(self, sid: str, **kwargs):
        with self._lock:
            if sid in self._metadata:
                self._metadata[sid].update(kwargs)

    def list_sessions(self, token_hash: str = "") -> list[dict]:
        with self._lock:
            if token_hash:
                allowed = self._token_sessions.get(token_hash, set())
                return [
                    {"session_id": sid, **meta}
                    for sid, meta in self._metadata.items()
                    if sid in allowed
                ]
            return [
                {"session_id": sid, **meta}
                for sid, meta in self._metadata.items()
            ]

    def get_meta(self, sid: str) -> dict | None:
        with self._lock:
            return self._metadata.get(sid)

    def delete(self, sid: str) -> bool:
        with self._lock:
            agent = self._sessions.pop(sid, None)
            meta = self._metadata.pop(sid, None)
            if meta and "token_hash" in meta:
                th = meta["token_hash"]
                if th in self._token_sessions:
                    self._token_sessions[th].discard(sid)
                    if not self._token_sessions[th]:
                        del self._token_sessions[th]
        # Shut down OUTSIDE the lock — agent.shutdown() may block
        # on network I/O and cannot hold the session lock.
        if agent:
            self._shutdown_agent(agent)
        return agent is not None
