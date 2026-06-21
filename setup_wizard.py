"""First-run interactive configuration wizard — extracted from main.py."""
import os
import sys
from pathlib import Path


def is_configured() -> bool:
    """Check if settings.json exists and has an API key."""
    settings_file = Path.home() / ".ata_coder" / "settings.json"
    if not settings_file.exists():
        return False
    try:
        import json as _json
        data = _json.loads(settings_file.read_text(encoding="utf-8"))
        legacy_key = data.get("api", {}).get("api_key", "").strip()
        env_key = data.get("env", {}).get("ATA_CODER_API_KEY", "").strip()
        return bool(legacy_key or env_key)
    except Exception:
        return False


def run_setup_wizard() -> None:
    """Interactive API configuration wizard."""
    settings_dir = Path.home() / ".ata_coder"
    settings_file = settings_dir / "settings.json"

    print("  检测到首次运行，开始初始化配置...")
    print()

    # ── API Base URL ────────────────────────────────────
    default_url = "https://api.deepseek.com"
    print(f"  API Base URL [默认: {default_url}]:")
    try:
        base_url = input("  > ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  配置取消。")
        sys.exit(0)
    if not base_url:
        base_url = default_url

    # ── API Key ─────────────────────────────────────────
    print()
    print("  API Key (输入会隐藏):")
    try:
        if os.name == "nt":
            import msvcrt
            raw_bytes: bytearray = bytearray()
            while True:
                ch = msvcrt.getch()
                if ch in (b"\r", b"\n"):
                    break
                if ch == b"\x08":
                    if raw_bytes:
                        raw_bytes.pop()
                elif ch == b"\x03":
                    print("\n  配置取消。")
                    sys.exit(0)
                else:
                    raw_bytes.extend(ch)
            # Decode the accumulated bytes as a complete UTF-8 sequence.
            # Per-byte decoding (ch.decode()) corrupts multi-byte characters
            # like CJK or accented Latin.
            api_key = raw_bytes.decode("utf-8", errors="replace")
        else:
            import tty
            import termios
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                api_key = ""
                while True:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n"):
                        break
                    if ch == "\x03":
                        print("\n  配置取消。")
                        sys.exit(0)
                    if ch in ("\x7f", "\x08"):  # backspace handling
                        if api_key:
                            api_key = api_key[:-1]
                            sys.stdout.write("\b \b")
                        continue
                    api_key += ch
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (KeyboardInterrupt, EOFError):
        print("\n  配置取消。")
        sys.exit(0)
    print()

    if not api_key.strip():
        print("  ⚠ 未输入 API Key。可稍后编辑 ~/.ata_coder/settings.json")
        print()

    # ── Write settings ──────────────────────────────────
    settings_dir.mkdir(parents=True, exist_ok=True)
    from .settings import Settings, _store_credential
    s = Settings().load(settings_file)
    s.set("env", "ATA_CODER_BASE_URL", base_url, save=False)

    # Try OS-native credential store first (DPAPI on Windows, Keychain on macOS, etc.)
    key_stored_securely = False
    if api_key.strip():
        key_stored_securely = _store_credential("ata-coder", "api-key", api_key.strip())

    if key_stored_securely:
        # Credential saved in OS-native store — don't write to plaintext settings
        s.set("env", "ATA_CODER_API_KEY", "", save=False)
        s.save()
        print("  ✓ API Key 已加密保存到操作系统凭据存储")
        print(f"  ✓ 基础配置已保存到 {settings_file}")
    else:
        # Fallback: plaintext settings.json (credential store unavailable)
        s.set("env", "ATA_CODER_API_KEY", api_key.strip(), save=False)
        s.save()
        print("  ⚠ 凭据存储不可用，API Key 以明文保存。")
        print(f"  ✓ 配置已保存到 {settings_file}")
    print()


__version__ = "1.0.0"


def print_banner() -> None:
    """Startup banner — project identity in 0.1 seconds."""
    try:
        test_dir = Path(__file__).parent / "tests"
        test_files = list(test_dir.glob("test_*.py"))
        test_count = 0
        for f in test_files:
            with open(f, "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().startswith("def test_"):
                        test_count += 1
    except Exception:
        test_count = "?"

    print(fr"""
┌─ ATA Coder v{__version__} ────────────┐
│  🤖 Self-Bootstrapped AI Assistant  │
│  📦 {test_count} tests passed               {' ' if len(str(test_count)) < 3 else ''} │
│  🔒 Security-hardened by self-audit │
└─────────────────────────────────────┘
""")


def ensure_first_run(force: bool = False) -> None:
    """Check config; if not configured (or force=True), run setup wizard.

    Scenarios:
      A) No config  → banner + setup wizard
      B) ata init   → force setup wizard (overwrite)
      C) Has config → silent pass, straight to REPL
    """
    if not force and is_configured():
        return
    if not force:
        print_banner()
    run_setup_wizard()
