"""
Central settings management for ATA Coder.

Stores all persistent configuration in ~/.ata_coder/settings.json.
The ``env`` section is the canonical source for provider configuration
(base URL, API key, model mapping, tokens, effort level) — it mirrors
the Claude Code settings format so a single file works for both tools.

Legacy ``api`` / ``model`` / ``vision`` top-level keys are still
respected as fallbacks, but new config should go into ``env``.

Usage:
    from .settings import get_settings
    s = get_settings()
    model = s.model_for(task)  # auto-route based on complexity
"""

import json
import logging
import os
import shutil
import sys
import sysconfig
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Default settings ──────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict[str, Any] = {
    "env": {
        # Provider-agnostic configuration (Claude Code compatible).
        # These are the canonical keys — everything else falls back here.
        "ATA_CODER_BASE_URL": "https://api.deepseek.com",
        "ATA_CODER_API_KEY": "",
        "ATA_CODER_DEFAULT_MODEL": "deepseek-v4-pro",
        "ATA_CODER_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
        "ATA_CODER_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "ATA_CODER_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "ATA_CODER_SUBAGENT_MODEL": "deepseek-v4-flash",
        "ATA_CODER_MAX_OUTPUT_TOKENS": "16384",
        "ATA_CODER_EFFORT_LEVEL": "",
        # Context compression (v2.6+): "llmlingua" | "llm" | "auto"
        "ATA_CODER_COMPRESSION_METHOD": "auto",
        # LLMLingua opt-in (set "1" to enable local compression)
        "ATA_CODER_LLMLINGUA": "",
        # Vision config (empty = inherit from main model/API)
        "ATA_CODER_VISION_MODEL": "",
        "ATA_CODER_VISION_API_BASE": "",
        "ATA_CODER_VISION_API_KEY": "",
    },
    "vision": {
        # Override vision-specific provider/model (empty = inherit from env).
        "model": "",
        "api_base": "",
        "api_key": "",
    },
    "complexity": {
        "auto_detect": True,
        "simple_max_chars": 60,
        "complex_min_chars": 500,
    },
    "paths": {
        "data": "~/.ata_coder",
        "skills": "~/.ata_coder/skills",
        "sessions": "~/.ata_coder/sessions",
        "memory": "~/.ata_coder/memory",
        "changes": "~/.ata_coder/changes",
    },
}


# ── Settings class ────────────────────────────────────────────────────────────

@dataclass
class Settings:
    """Singleton settings manager backed by ~/.ata_coder/settings.json."""

    _data: dict[str, Any] = field(default_factory=dict)
    _file: Path | None = None

    # ── Init / Load / Save ──────────────────────────────────────────────────

    def load(self, file_path: str | Path | None = None) -> "Settings":
        """Load settings from disk, creating defaults if needed."""
        if file_path:
            self._file = Path(file_path)
        else:
            self._file = Path.home() / ".ata_coder" / "settings.json"

        self._file.parent.mkdir(parents=True, exist_ok=True)

        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                from .utils import deep_merge_dict
                self._data = deep_merge_dict(DEFAULT_SETTINGS, loaded)
                logger.debug("Loaded settings from %s", self._file)
            except Exception as e:
                logger.warning("Failed to load %s, using defaults: %s", self._file, e)
                # Back up the corrupt file before overwriting
                try:
                    _backup = self._file.with_suffix(".json.corrupt")
                    import shutil as _shutil
                    _shutil.copy2(str(self._file), str(_backup))
                    logger.warning("Corrupt settings backed up to %s", _backup)
                except Exception:
                    pass
                self._data = dict(DEFAULT_SETTINGS)
                self.save()
        else:
            self._data = dict(DEFAULT_SETTINGS)
            self.save()
            logger.info("Created default settings at %s", self._file)

        return self

    def save(self) -> None:
        """Persist current settings to disk.

        Only writes keys that are in DEFAULT_SETTINGS — extra fields that
        were merged in from disk (e.g. Claude Code hooks, plugins) are
        carried forward because ``deep_merge_dict`` preserves them in ``_data``,
        but ``save()`` writes the full ``_data`` dict.  If you want to
        strip unknown keys, delete them via ``set(key, None)`` first.
        """
        if not self._file:
            return
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, ensure_ascii=False)
            # Restrict permissions: owner-only read/write (protects API keys)
            os.chmod(self._file, 0o600)
        except Exception as e:
            logger.error("Failed to save settings: %s", e)

    def reload(self) -> None:
        """Reload settings from disk."""
        self.load(self._file)

    # ── Access ──────────────────────────────────────────────────────────────

    def get(self, *keys: str, default: Any = None) -> Any:
        """Get a nested key, e.g. settings.get('model', 'default')."""
        node = self._data
        for k in keys:
            if isinstance(node, dict):
                node = node.get(k)
            else:
                return default
            if node is None:
                return default
        return node

    def set(self, *keys: str, value: Any, save: bool = True) -> None:
        """Set a nested key, e.g. settings.set('model', 'default', 'gpt-4o')."""
        node = self._data
        for k in keys[:-1]:
            if k not in node or not isinstance(node[k], dict):
                node[k] = {}
            node = node[k]
        node[keys[-1]] = value
        if save:
            self.save()

    @property
    def data(self) -> dict:
        """Return a shallow copy to prevent accidental mutation."""
        return dict(self._data)

    # ── Path helpers ────────────────────────────────────────────────────────

    def resolve_path(self, key: str) -> Path:
        """Resolve a path from settings (expanding ~)."""
        raw = self.get("paths", key, default=f"~/.ata_coder/{key}")
        return Path(raw).expanduser().resolve()

    # ── env section (canonical) ─────────────────────────────────────────────
    # Every provider-adjacent property reads ``env`` FIRST, then falls back
    # to the legacy ``api`` / ``model`` / ``vision`` sections, then to a
    # hardcoded default.  This way a single ``env`` block is sufficient, but
    # old settings files without ``env`` still work.

    def _env_val(self, key: str, default: str = "") -> str:
        """Read a value from the ``env`` section."""
        return self.get("env", key, default=default)

    # ── API ─────────────────────────────────────────────────────────────────

    @property
    def api_base_url(self) -> str:
        return (
            self._env_val("ATA_CODER_BASE_URL")
            or self.get("api", "base_url", default="")       # legacy
            or "https://api.deepseek.com"
        )

    @property
    def api_key(self) -> str:
        return (
            self._env_val("ATA_CODER_API_KEY")
            or self.get("api", "api_key", default="")        # legacy
        )

    # ── Model routing ───────────────────────────────────────────────────────

    @property
    def default_model(self) -> str:
        return (
            self._env_val("ATA_CODER_DEFAULT_MODEL")
            or self.get("model", "default", default="")      # legacy
            or "deepseek-v4-pro"
        )

    @property
    def model_opus(self) -> str:
        return (
            self._env_val("ATA_CODER_DEFAULT_OPUS_MODEL")
            or self.get("model", "mapping", "opus", default="")  # legacy
            or self.default_model
        )

    @property
    def model_sonnet(self) -> str:
        return (
            self._env_val("ATA_CODER_DEFAULT_SONNET_MODEL")
            or self.get("model", "mapping", "sonnet", default="")
            or self.default_model
        )

    @property
    def model_haiku(self) -> str:
        return (
            self._env_val("ATA_CODER_DEFAULT_HAIKU_MODEL")
            or self.get("model", "mapping", "haiku", default="")
            or self.default_model
        )

    @property
    def model_subagent(self) -> str:
        return (
            self._env_val("ATA_CODER_SUBAGENT_MODEL")
            or self.get("model", "mapping", "subagent", default="")
            or self.default_model
        )

    @property
    def max_output_tokens(self) -> int:
        try:
            return int(self._env_val("ATA_CODER_MAX_OUTPUT_TOKENS", "16384"))
        except (ValueError, TypeError):
            return 16384

    @property
    def effort_level(self) -> str:
        return self._env_val("ATA_CODER_EFFORT_LEVEL")

    @property
    def use_anthropic(self) -> bool:
        """Whether to use Anthropic Messages API format (instead of OpenAI)."""
        return self._env_val("ATA_CODER_USE_ANTHROPIC") == "1"

    # ── Vision (still uses dedicated section — optional override) ───────────

    @property
    def vision_model(self) -> str:
        """Vision model override (empty = use main model from ATA_CODER_DEFAULT_MODEL)."""
        return self._env_val("ATA_CODER_VISION_MODEL") or self.get("vision", "model", default="")

    @property
    def vision_api_base(self) -> str:
        """Vision API base override (empty = use main ATA_CODER_BASE_URL)."""
        return self._env_val("ATA_CODER_VISION_API_BASE") or self.get("vision", "api_base", default="")

    @property
    def vision_api_key(self) -> str:
        """Vision API key override (empty = use main ATA_CODER_API_KEY)."""
        return self._env_val("ATA_CODER_VISION_API_KEY") or self.get("vision", "api_key", default="")

    # ── Directories ─────────────────────────────────────────────────────────

    @property
    def data_dir(self) -> Path:
        return self.resolve_path("data")

    @property
    def skills_dir(self) -> Path:
        return self.resolve_path("skills")

    @property
    def sessions_dir(self) -> Path:
        return self.resolve_path("sessions")

    @property
    def memory_dir(self) -> Path:
        return self.resolve_path("memory")

    @property
    def changes_dir(self) -> Path:
        return self.resolve_path("changes")

    def ensure_dirs(self) -> None:
        """Create all configured directories."""
        for key in ("data", "skills", "sessions", "memory", "changes"):
            d = self.resolve_path(key)
            d.mkdir(parents=True, exist_ok=True)

    # ── Complexity ──────────────────────────────────────────────────────────

    # shortcut_classify moved → model_router.py (ModelRouter.classify_shortcut)
    # _deep_merge moved → utils.py (deep_merge_dict)


# ── Global singleton ──────────────────────────────────────────────────────────

_settings: Settings | None = None
_settings_lock = threading.Lock()


def get_settings(file_path: str | Path | None = None) -> Settings:
    """Get or create the global Settings singleton (thread-safe)."""
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings().load(file_path)
    return _settings


def init_settings(file_path: str | Path | None = None) -> Settings:
    """Initialize settings, seeding skills from project source if needed."""
    settings = get_settings(file_path)

    # Ensure all directories exist
    settings.ensure_dirs()

    # Seed default skills from project source → ~/.ata_coder/skills/
    _seed_skills(settings)

    # Seed default memories from project source → ~/.ata_coder/memory/
    _seed_memories(settings)

    return settings


def _find_source_dir(name: str) -> Path | None:
    """Find a data directory (skills, memory, prompts) in various locations."""
    # 1. Development: next to this file (project root)
    candidate = Path(__file__).parent / name
    if candidate.is_dir():
        return candidate
    # 2. pip install: site-packages data_files
    data_path = Path(sysconfig.get_path("data"))
    candidate = data_path / name
    if candidate.is_dir():
        return candidate
    # 3. pip install (user): user site data
    user_data = Path(sysconfig.get_path("data", scheme="nt_user" if os.name == "nt" else "posix_user"))
    candidate = user_data / name
    if candidate.is_dir():
        return candidate
    return None


def _seed_skills(settings: Settings) -> None:
    """Copy skill folders from project source to ~/.ata_coder/skills/ if empty."""
    target = settings.skills_dir

    # Check if skills already exist (folder-based or flat legacy)
    has_skills = any(
        (d / "SKILL.md").exists() or (d / "manifest.json").exists()
        for d in target.iterdir() if d.is_dir()
    )
    if has_skills:
        return

    source = _find_source_dir("skills")
    if not source:
        logger.debug("No skills source dir found, skipping seed")
        return

    target.mkdir(parents=True, exist_ok=True)
    for d in source.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            dest = target / d.name
            if not dest.exists():
                shutil.copytree(d, dest)
                logger.info("Seeded skill folder: %s", d.name)
    for fp in source.glob("*.md"):
        dest = target / fp.name
        if not dest.exists():
            shutil.copy2(fp, dest)
            logger.info("Seeded skill: %s", fp.name)
    for fp in source.glob("*.json"):
        dest = target / fp.name
        if not dest.exists():
            shutil.copy2(fp, dest)


def _seed_memories(settings: Settings) -> None:
    """Copy default memory files from project source to ~/.ata_coder/memory/ if empty."""
    target = settings.memory_dir
    if list(target.glob("*.md")):
        return

    source = _find_source_dir("memory")
    if not source:
        logger.debug("No memory source dir found, skipping seed")
        return

    target.mkdir(parents=True, exist_ok=True)
    for fp in source.glob("*"):
        if fp.name == "__pycache__":
            continue
        if fp.is_file():
            shutil.copy2(fp, target / fp.name)
            logger.info("Seeded memory: %s → %s", fp.name, target / fp.name)


# ── Credential Store ──────────────────────────────────────────────────────────
# Prefer OS-native credential stores over plaintext settings.json.
# Falls back gracefully when native tools are unavailable.

def _credential_file() -> Path | None:
    """Path to the DPAPI-encrypted credential file (Windows only).

    Returns None when the home directory is unavailable (e.g., test environments).
    """
    try:
        home = Path.home()
    except RuntimeError:
        return None
    return home / ".ata_coder" / ".credential"


def _get_credential(service: str, account: str) -> str | None:
    """Try to read a credential from OS-native stores. Returns None if unavailable."""
    if os.name == "nt":
        # ── Windows: DPAPI via PowerShell (built-in, zero dependencies) ─────
        # DPAPI encrypts with the user's login session — only the same user on
        # the same machine can decrypt.  The encrypted blob is stored as base64
        # in ~/.ata_coder/.credential
        cred_file = _credential_file()
        if cred_file is None or not cred_file.exists():
            return None
        try:
            import subprocess
            b64_data = cred_file.read_text(encoding="utf-8").strip()
            ps_script = (
                'Add-Type -AssemblyName System.Security;'
                f'$protected = [System.Convert]::FromBase64String("{b64_data}");'
                '$bytes = [System.Security.Cryptography.ProtectedData]::Unprotect('
                '$protected, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser);'
                '[System.Text.Encoding]::UTF8.GetString($bytes)'
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    elif sys.platform == "darwin":
        # macOS: Keychain via security CLI (built-in)
        try:
            import subprocess
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    else:
        # Linux: try secret-tool (libsecret)
        try:
            import subprocess
            result = subprocess.run(
                ["secret-tool", "lookup", "service", service, "account", account],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    return None


def _store_credential(service: str, account: str, secret: str) -> bool:
    """Try to store a credential in OS-native stores. Returns True on success."""
    if os.name == "nt":
        # ── Windows: DPAPI encrypt → store base64 blob in ~/.ata_coder/.credential
        try:
            import base64
            import subprocess
            # Base64-encode the secret in Python to avoid f-string injection
            # (secrets containing '{' would break f-string formatting).
            b64_secret = base64.b64encode(secret.encode("utf-8")).decode("ascii")
            ps_script = (
                'Add-Type -AssemblyName System.Security;'
                f'$bytes = [System.Convert]::FromBase64String("{b64_secret}");'
                '$protected = [System.Security.Cryptography.ProtectedData]::Protect('
                '$bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser);'
                '[System.Convert]::ToBase64String($protected)'
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                cred_file = _credential_file()
                if cred_file is None:
                    return False
                cred_file.parent.mkdir(parents=True, exist_ok=True)
                cred_file.write_text(result.stdout.strip(), encoding="utf-8")
                # Restrictive permissions on the credential file
                try:
                    cred_file.chmod(0o600)
                except Exception:
                    pass
                return True
        except Exception:
            pass
    elif sys.platform == "darwin":
        # macOS: Keychain via security CLI (built-in)
        try:
            import subprocess
            result = subprocess.run(
                ["security", "add-generic-password", "-s", service, "-a", account,
                 "-w", secret, "-U"],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            pass
    else:
        # Linux: try secret-tool
        try:
            import subprocess
            result = subprocess.run(
                ["secret-tool", "store", "--label", f"ATA Coder ({account})",
                 "service", service, "account", account],
                input=secret, capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            pass
    return False


def _delete_credential(service: str, account: str) -> bool:
    """Remove a stored credential. Returns True on success or if it didn't exist."""
    if os.name == "nt":
        cred_file = _credential_file()
        if cred_file is None:
            return False
        try:
            cred_file.unlink(missing_ok=True)
            return True
        except Exception:
            return False
    elif sys.platform == "darwin":
        try:
            import subprocess
            result = subprocess.run(
                ["security", "delete-generic-password", "-s", service, "-a", account],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False
    else:
        try:
            import subprocess
            result = subprocess.run(
                ["secret-tool", "clear", "service", service, "account", account],
                capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False


def resolve_api_key(settings: "Settings | None" = None) -> str:
    """Resolve API key with tiered fallback:
    1. OS-native credential store (DPAPI/Keychain/secret-tool)
    2. ATA_CODER_API_KEY environment variable
    3. OPENAI_API_KEY environment variable
    4. settings.json (plaintext) — auto-migrated to credential store when possible
    """
    # Tier 1: OS-native credential store
    credential = _get_credential("ata-coder", "api-key")
    if credential:
        return credential

    # Tier 2-3: Environment variables
    for env_var in ("ATA_CODER_API_KEY", "OPENAI_API_KEY"):
        val = os.environ.get(env_var, "")
        if val:
            return val

    # Tier 4: settings.json (plaintext fallback)
    if settings is None:
        try:
            settings = get_settings()
        except Exception:
            return ""
    key = settings.api_key
    if key:
        # Auto-migrate: if credential store is available, move key out of plaintext
        if _store_credential("ata-coder", "api-key", key):
            # Clear the plaintext key from settings.json
            try:
                settings.set("env", "ATA_CODER_API_KEY", "", save=True)
            except Exception:
                pass
            logger.info(
                "🔐 API key migrated from plaintext settings.json to OS credential store. "
                "Your key is now encrypted at rest."
            )
            return key
        # Credential store unavailable — warn but still return the key
        logger.warning(
            "⚠️  API key stored in plaintext settings.json. "
            "Consider using the ATA_CODER_API_KEY environment variable "
            "or an OS-native credential store instead."
        )
    return key
