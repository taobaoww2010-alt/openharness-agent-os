"""Shared hidden Chrome session — one window, one tab, reused across tool calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time

_logger = logging.getLogger(__name__)

CUA_DRIVER = shutil.which("cua-driver") or "cua-driver"

_NAVIGATE_TIMEOUT = 15.0
_CUA_TIMEOUT = 30.0


class ChromeSession:
    """Singleton — one hidden Chrome window shared across web_search / web_fetch.

    Usage::

        session = await ChromeSession.get()
        await session.navigate("https://example.com")
        text = await session.get_text()
    """

    _pid: int | None = None
    _window_id: int | None = None
    _lock = asyncio.Lock()

    # ── public API ──────────────────────────────────────────────────────

    @classmethod
    async def get(cls) -> ChromeSession:
        """Return the shared session, starting Chrome if needed."""
        if cls._pid is not None:
            return cls
        async with cls._lock:
            if cls._pid is not None:  # double-check
                return cls
            await cls._start()
        return cls

    @classmethod
    async def navigate(cls, url: str) -> None:
        """Navigate the shared tab to *url* and wait for DOM ready.

        Uses ``window.location.href`` first; if the page remains on a Chrome
        internal page (error page, about:blank, etc.), falls back to
        ``open -a`` which always works.
        """
        await cls._ensure_running()
        # Check if we are on a chrome internal page — those block JS navigation
        current = await cls.execute_js("window.location.href")
        cur_url = _extract_plain_output(current) or ""
        if cur_url.startswith(("chrome-error://", "chrome://", "about:")):
            await cls._navigate_via_open(url)
            return

        js = f"window.location.href = {json.dumps(url)}"
        await cls._cua(["page", json.dumps({
            "pid": cls._pid,
            "window_id": cls._window_id,
            "action": "execute_javascript",
            "javascript": js,
        })])
        # Poll document.readyState instead of a fixed sleep
        await asyncio.sleep(0.5)  # give navigation time to start
        deadline = time.monotonic() + _NAVIGATE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                out = await cls._cua(["page", json.dumps({
                    "pid": cls._pid,
                    "window_id": cls._window_id,
                    "action": "execute_javascript",
                    "javascript": "document.readyState",
                })])
                state = _extract_plain_output(out) or "unknown"
                if "complete" in state:
                    if await cls._check_navigation(url, cur_url):
                        return
                    await cls._navigate_via_open(url)
                    return
                if "interactive" in state:
                    await asyncio.sleep(0.3)
                    continue
            except Exception:
                pass
            await asyncio.sleep(0.5)
        # Fallback: wait and check final URL
        await asyncio.sleep(2)
        if not await cls._check_navigation(url, cur_url):
            await cls._navigate_via_open(url)

    @classmethod
    async def _check_navigation(cls, target_url: str, old_url: str) -> bool:
        """Return True if we successfully navigated away from *old_url* to *target_url*."""
        out = await cls.execute_js("window.location.href")
        current = _extract_plain_output(out) or ""
        if current.startswith(("chrome-error://", "about:")):
            return False
        # Already on the target → success
        if current == target_url:
            return True
        # URL changed from old → navigation happened (redirects, etc.)
        if old_url and current != old_url:
            return True
        # URL didn't change → silent failure
        return False

    @classmethod
    async def _navigate_via_open(cls, url: str) -> None:
        """Open *url* via ``open -a`` (always works, opens a new tab)."""
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/open", "-a", "Google Chrome", url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        await asyncio.sleep(1.5)  # wait for new tab to be ready
        # Poll readyState
        deadline = time.monotonic() + _NAVIGATE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                out = await cls._cua(["page", json.dumps({
                    "pid": cls._pid,
                    "window_id": cls._window_id,
                    "action": "execute_javascript",
                    "javascript": "document.readyState",
                })])
                state = _extract_plain_output(out) or "unknown"
                if "complete" in state:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)

    @classmethod
    async def execute_js(cls, js: str) -> str:
        """Execute JavaScript in the shared tab and return the result."""
        await cls._ensure_running()
        out = await cls._cua(["page", json.dumps({
            "pid": cls._pid,
            "window_id": cls._window_id,
            "action": "execute_javascript",
            "javascript": js,
        })])
        return out

    @classmethod
    async def get_text(cls) -> str:
        """Return ``document.body.innerText`` of the shared tab."""
        await cls._ensure_running()
        out = await cls._cua(["page", json.dumps({
            "pid": cls._pid,
            "window_id": cls._window_id,
            "action": "get_text",
        })])
        return out

    @classmethod
    async def close(cls) -> None:
        """Close the shared tab (best-effort)."""
        if cls._pid is None:
            return
        try:
            await cls._cua(["hotkey", json.dumps({
                "pid": cls._pid,
                "keys": ["cmd", "w"],
            })])
        except Exception:
            _logger.warning("Failed to close shared Chrome tab", exc_info=True)
        cls._pid = None
        cls._window_id = None

    # ── internal helpers ────────────────────────────────────────────────

    @classmethod
    async def _ensure_running(cls) -> None:
        if cls._pid is None:
            await cls.get()
            return
        # Verify Chrome process is still alive; re-launch if gone
        try:
            os.kill(cls._pid, 0)
        except ProcessLookupError:
            _logger.warning("Chrome pid=%s is gone — re-launching", cls._pid)
            cls._pid = None
            cls._window_id = None
            await cls.get()

    @classmethod
    async def _find_existing_chrome(cls) -> bool:
        """Look for an already-running Chrome process and pick a window."""
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/pgrep", "-x", "Google Chrome",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        pid_str = stdout.decode().strip()
        if not pid_str:
            return False
        try:
            pid = int(pid_str.split("\n")[0])
        except (ValueError, IndexError):
            return False
        # Get windows for this pid
        try:
            out = await cls._cua(["list_windows", json.dumps({"pid": pid})])
            data = json.loads(out)
            windows = data.get("windows", []) if isinstance(data, dict) else []
        except Exception:
            windows = []
        if not windows:
            return False
        # Pick the best window: prefer visible, on current space, with meaningful content
        best = None
        for w in windows:
            if w.get("is_on_screen") and w.get("on_current_space"):
                bounds = w.get("bounds", {})
                ww, wh = bounds.get("width", 0), bounds.get("height", 0)
                if ww > 200 and wh > 100:
                    best = w
                    break
        if best is None:
            # Fallback: any non-minimal window
            for w in windows:
                bounds = w.get("bounds", {})
                ww, wh = bounds.get("width", 0), bounds.get("height", 0)
                if ww > 200 and wh > 100:
                    best = w
                    break
        if best is None:
            best = windows[-1]  # last resort
        cls._pid = pid
        cls._window_id = best["window_id"]
        _logger.info("Reused existing Chrome pid=%s window_id=%s", cls._pid, cls._window_id)
        return True

    @classmethod
    async def _start(cls) -> None:
        """Launch Chrome or reuse existing instance."""
        # First, try to reuse an already-running Chrome
        if await cls._find_existing_chrome():
            return

        _logger.info("Launching shared Chrome session (hidden, about:blank)")
        # Try cua-driver launch_app first
        try:
            out = await cls._cua(["launch_app", json.dumps({
                "bundle_id": "com.google.chrome",
                "urls": ["about:blank"],
            })])
            info = json.loads(out)
            cls._pid = info["pid"]
            windows = info.get("windows", [])
        except Exception as exc:
            _logger.warning("launch_app failed (%s), trying open -a", exc)
            # Fallback: use open -a (works even when cua-driver TCC is misattributed)
            proc2 = await asyncio.create_subprocess_exec(
                "/usr/bin/open", "-a", "Google Chrome", "--args", "about:blank",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc2.wait()
            await asyncio.sleep(2)
            # Find Chrome pid now that it's running
            if await cls._find_existing_chrome():
                return
            raise RuntimeError(f"Cannot start Chrome: {exc}")

        if not windows:
            await asyncio.sleep(2)
            try:
                out2 = await cls._cua(["list_windows", json.dumps({"pid": cls._pid})])
                info2 = json.loads(out2)
                windows = info2
            except Exception:
                windows = []
        if not windows:
            raise RuntimeError("Chrome launched but no window appeared")
        cls._window_id = windows[-1]["window_id"]
        _logger.info("Shared Chrome session ready: pid=%s window_id=%s", cls._pid, cls._window_id)

    @classmethod
    async def _cua(cls, args: list[str]) -> str:
        """Run *cua-driver* with *args* and return stdout."""
        proc = await asyncio.create_subprocess_exec(
            CUA_DRIVER, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CUA_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                err = stderr.decode().strip()
                if err:
                    raise RuntimeError(err)
                else:
                    raise RuntimeError(f"exit code {proc.returncode}")
            return stdout.decode()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


def _extract_plain_output(output: str) -> str | None:
    """Extract plain-text result from cua-driver's markdown output.

    Handles formats like::

        ## Result

        ```
        <content>
        ```
    """
    m = re.search(r"^## Result\s*\n\s*```\s*\n(.*?)\n\s*```", output, re.DOTALL | re.MULTILINE)
    if m:
        return m.group(1).strip()
    # Fallback: return everything after "## Result"
    m2 = re.search(r"## Result\s*\n+(.*)", output, re.DOTALL)
    if m2:
        return m2.group(1).strip()
    return None
