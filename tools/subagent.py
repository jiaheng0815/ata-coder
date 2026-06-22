"""Sub-agent, MCP search, and vision/image analysis — mixin for ToolExecutor."""
import asyncio
import html
import html.parser
import logging
import os
import re

from .result import ToolResult  # noqa: E402 — circular-safe

logger = logging.getLogger(__name__)


def _get_clawd():
    """Lazy-import clawd to avoid breaking the tools package if clawd is unavailable."""
    try:
        from ..clawd_integration import get_clawd
        return get_clawd()
    except Exception:
        return None

class SubAgentToolsMixin:
    """Sub-agent, MCP, and vision capabilities for ToolExecutor."""

    # ── Sub-agent tools ──────────────────────────────────────────────────

    async def _tool_spawn_subagent(self, task: str, skill: str = "",
                             model: str = "") -> ToolResult:
        """Spawn a sub-agent to work on a task in parallel."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available. "
                      "Ensure agent_controller is used.",
            )
        try:
            # Clawd: SubagentStart
            clawd = _get_clawd()
            if clawd:
                clawd.subagent_start()

            agent_id = await self._sub_agent_mgr.spawn(
                task=task,
                skill_prompt=skill,
                model=model or None,
            )
            return ToolResult(
                success=True,
                output=(
                    f"Sub-agent spawned: {agent_id}\n"
                    f"Status: running\n"
                    f"Active sub-agents: {self._sub_agent_mgr.active_count}\n\n"
                    f"Use collect_subagent('{agent_id}') to retrieve results, "
                    f"or list_subagents() to check all statuses."
                ),
            )
        except RuntimeError as e:
            return ToolResult(
                success=False, output="",
                error=f"Cannot spawn sub-agent: {e}",
            )

    async def _tool_collect_subagent(self, agent_id: str,
                                timeout: float = 300.0) -> ToolResult:
        """Collect results from a spawned sub-agent."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available.",
            )
        result = await self._sub_agent_mgr.collect(agent_id, timeout=timeout)

        # Clawd: SubagentStop
        clawd = _get_clawd()
        if clawd:
            clawd.subagent_stop()

        if result.success:
            lines = [
                f"Sub-agent {agent_id} completed successfully.",
                f"Tool calls: {result.tool_call_count}",
                "",
                "Result:",
                result.result or "(empty)",
            ]
            return ToolResult(success=True, output="\n".join(lines))
        return ToolResult(
            success=False,
            output=f"Sub-agent {agent_id} failed: {result.error}",
            error=result.error,
        )

    async def _tool_list_subagents(self) -> ToolResult:
        """List all sub-agents and their statuses."""
        if not self._sub_agent_mgr:
            return ToolResult(
                success=False, output="",
                error="SubAgentManager not available.",
            )
        agents = self._sub_agent_mgr.list_all()
        if not agents:
            return ToolResult(success=True, output="No sub-agents.")

        lines = [f"Sub-agents ({len(agents)} total):", ""]
        for a in agents:
            status_icon = {"running": "🔄", "done": "✅",
                           "failed": "❌", "cancelled": "⏹️"}.get(a.status, "❓")
            lines.append(
                f"  {status_icon} {a.id} — {a.status} "
                f"(tool_calls={a.tool_call_count})"
            )
        return ToolResult(success=True, output="\n".join(lines))

    async def _tool_mcp_search(self, query: str, type: str = "all") -> ToolResult:
        """Search MCP tools and resources across all connected servers."""
        if not self._mcp:
            return ToolResult(
                success=False, output="",
                error="MCP not configured. Add MCP servers via --mcp-config.",
            )

        servers = self._mcp.connected_servers
        if not servers:
            return ToolResult(success=True, output="No MCP servers connected.")

        lines = [f"MCP search results for '{query}' across {len(servers)} server(s):", ""]
        found = 0

        if type in ("tools", "all"):
            tools = self._mcp.search_tools(query, limit=20)
            if tools:
                lines.append(f"  Tools ({len(tools)}):")
                for t in tools:
                    name = t.get("name", "?")
                    desc = (t.get("description") or "")[:100]
                    server = t.get("_mcp_server", "?")
                    lines.append(f"    ● {name}  @{server}")
                    if desc:
                        lines.append(f"      {desc}")
                found += len(tools)
            else:
                lines.append("  Tools: none found")

        if type in ("resources", "all"):
            resources = self._mcp.search_resources(query, limit=20)
            if resources:
                lines.append(f"\n  Resources ({len(resources)}):")
                for r in resources:
                    uri = r.get("uri", "?")
                    name = r.get("name", "")
                    desc = (r.get("description") or "")[:80]
                    server = r.get("_mcp_server", "?")
                    label = name or uri
                    lines.append(f"    ● {label}  @{server}")
                    if desc:
                        lines.append(f"      {desc}")
                found += len(resources)
            else:
                lines.append("\n  Resources: none found")

        if found == 0:
            return ToolResult(
                success=True,
                output=f"No MCP tools or resources found matching '{query}'.\n"
                       f"Connected servers: {', '.join(servers)}.",
            )

        return ToolResult(success=True, output="\n".join(lines))

    async def _tool_analyze_image(self, image_path: str, prompt: str = "Describe this image in detail.") -> ToolResult:
        """Analyze an image using a multimodal vision model.

        Uses the configured vision model, falling back to the main LLM config.
        Configure via ~/.ata_coder/settings.json:
          {"vision": {"model": "...", "api_base": "...", "api_key": "..."}}
        Or env vars: VISION_MODEL, VISION_API_BASE, VISION_API_KEY.
        """
        import base64

        img_path = self._resolve_path(image_path)
        if not img_path.exists():
            return ToolResult(
                success=False, output="",
                error=f"Image not found: {image_path}",
            )

        ext = img_path.suffix.lower()
        supported = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        if ext not in supported:
            return ToolResult(
                success=False, output="",
                error=f"Unsupported image format: {ext}. Supported: {', '.join(sorted(supported))}",
            )

        try:
            with open(img_path, "rb") as f:
                img_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to read image: {e}")

        # ── Resolve vision config ──
        # Priority: env var > settings.json > main api config
        from .settings import get_settings
        settings = get_settings()

        # API key: VISION_API_KEY env > settings.json vision.api_key > main api key
        api_key = (
            os.environ.get("VISION_API_KEY", "")
            or settings.vision_api_key
            or os.environ.get("ATA_CODER_API_KEY", "")
            or os.environ.get("OPENAI_API_KEY", "")
            or settings.api_key
        )
        if not api_key:
            return ToolResult(
                success=False, output="",
                error="No API key configured. Set ATA_CODER_API_KEY or add vision.api_key in ~/.ata_coder/settings.json.",
            )

        # API base: VISION_API_BASE env > settings.json vision.api_base > main base_url
        api_base = (
            os.environ.get("VISION_API_BASE", "")
            or settings.vision_api_base
            or os.environ.get("ATA_CODER_BASE_URL", "")
            or os.environ.get("OPENAI_BASE_URL", "")
            or settings.api_base_url
        )

        # Model: VISION_MODEL env > settings.json vision.model > main model
        model = (
            os.environ.get("VISION_MODEL", "")
            or settings.vision_model
            or os.environ.get("ATA_CODER_DEFAULT_MODEL", "")
            or os.environ.get("OPENAI_MODEL", "")
            or settings.default_model
        )

        mime = ext.replace("jpg", "jpeg").replace(".", "image/")
        body = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{img_b64}",
                        "detail": "auto"
                    }},
                ]
            }],
            "max_tokens": 2048,
            "temperature": 0.3,
        }

        try:
            import json as _json
            from urllib.request import Request, urlopen
            from urllib.error import HTTPError

            data = _json.dumps(body).encode("utf-8")
            req = Request(
                f"{api_base.rstrip('/')}/chat/completions",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            loop = asyncio.get_running_loop()

            def _do_vision_request():
                with urlopen(req, timeout=120) as resp:
                    return resp.read()

            resp_data = await loop.run_in_executor(None, _do_vision_request)
            result = _json.loads(resp_data.decode("utf-8"))
            content = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "(no response)")
            )
            usage = result.get("usage", {})
            tokens = usage.get("total_tokens", "?")
            return ToolResult(
                success=True,
                output=f"[Vision: {model} | {tokens} tokens]\n\n{content}",
            )
        except HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")[:300]
            return ToolResult(
                success=False, output="",
                error=f"Vision API error {e.code}: {error_body}",
            )
        except Exception as e:
            return ToolResult(
                success=False, output="",
                error=f"Vision API call failed: {e}",
            )

    @staticmethod
    def _extract_text(html_text: str, url: str = "") -> str:
        """Strip HTML down to readable text."""

        class _TextExtractor(html.parser.HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []
                self._skip_count = 0  # counter for nested skip-tags
                self._skip_tags = {"script", "style", "noscript", "iframe",
                                   "nav", "footer", "header", "aside"}
                self._block_tags = {"div", "p", "h1", "h2", "h3", "h4", "h5",
                                    "h6", "li", "tr", "section", "article",
                                    "pre", "blockquote", "table", "ul", "ol",
                                    "dl", "br", "hr"}

            def handle_starttag(self, tag, attrs):
                tag = tag.lower()
                if tag in self._skip_tags:
                    self._skip_count += 1
                elif tag in self._block_tags:
                    self.parts.append("\n")

            def handle_endtag(self, tag):
                tag = tag.lower()
                if tag in self._skip_tags and self._skip_count > 0:
                    self._skip_count -= 1
                elif tag in self._block_tags:
                    self.parts.append("\n")

            def handle_data(self, data):
                if self._skip_count == 0:
                    text = data.strip()
                    if text:
                        self.parts.append(text + " ")

        try:
            extractor = _TextExtractor()
            extractor.feed(html_text)
            raw = "".join(extractor.parts)
        except Exception:
            # Fallback: regex strip
            raw = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r'<style[^>]*>.*?</style>', '', raw, flags=re.DOTALL | re.IGNORECASE)
            raw = re.sub(r'<[^>]+>', ' ', raw)
            raw = html.unescape(raw)

        # Collapse whitespace
        raw = re.sub(r'[ \t]+', ' ', raw)
        raw = re.sub(r'\n{3,}', '\n\n', raw)
        return raw.strip()


