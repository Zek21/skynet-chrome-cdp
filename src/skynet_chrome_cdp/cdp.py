"""Chrome DevTools Protocol client for a browser that is already running.

DESIGN POSITION
---------------
Playwright and Puppeteer launch a browser. This library attaches to the one the
user is already signed into. That difference drives every design decision here:

  * The browser is not ours. It has the user's tabs open, possibly their bank,
    certainly their session cookies. So tab ownership is tracked explicitly and
    mutating a tab this process did not create requires opting in. See
    `TabOwnershipError`.

  * The browser outlives us. Connections are per-tab and closed deterministically,
    because a leaked WebSocket against a long-lived browser accumulates until
    Chrome starts refusing targets.

  * Attaching is a privileged act. Anything that can reach the DevTools port can
    read every cookie in the profile. `SECURITY.md` states the threat model; the
    short version is that the port MUST stay bound to loopback.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .transport import WebSocket, WebSocketError

__all__ = ["CDPError", "TabOwnershipError", "Tab", "Chrome"]

DEFAULT_PORT = 9222


class CDPError(Exception):
    """A CDP call returned an error, or the browser could not be reached."""


class TabOwnershipError(CDPError):
    """Refused to mutate a tab this process did not create.

    Raised instead of quietly acting on a user's tab. Pass `allow_foreign=True`
    to a mutating call when acting on someone else's tab is genuinely intended.
    """


def _http(port: int, path: str, method: str = "GET", timeout: float = 8.0) -> str:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urlopen(Request(url, method=method), timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise CDPError(f"{method} {path} -> HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise CDPError(
            f"no Chrome DevTools endpoint on 127.0.0.1:{port} ({exc}). "
            f"Start Chrome with --remote-debugging-port={port}."
        ) from exc


def _http_json(port: int, path: str, method: str = "GET", timeout: float = 8.0) -> Any:
    body = _http(port, path, method=method, timeout=timeout)
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except ValueError:
        # /json/close and /json/activate answer in plain text. Returning the raw
        # string is correct; raising here previously produced a false "could not
        # close the tab" warning for a tab that had in fact closed.
        return body.strip()


class Tab:
    """One attached tab. Each Tab owns exactly one WebSocket."""

    def __init__(self, chrome: "Chrome", info: dict, owned: bool = False):
        self._chrome = chrome
        self._info = dict(info)
        self._ws: WebSocket | None = None
        self._next_id = 0
        self.owned = owned

    # -- identity ----------------------------------------------------------
    @property
    def id(self) -> str:
        return self._info.get("id", "")

    @property
    def url(self) -> str:
        return self._info.get("url", "")

    @property
    def title(self) -> str:
        return self._info.get("title", "")

    def __repr__(self) -> str:
        flag = "owned" if self.owned else "foreign"
        return f"<Tab {self.id[:8]} {flag} {self.title[:40]!r}>"

    # -- connection --------------------------------------------------------
    def attach(self) -> "Tab":
        if self._ws is not None and self._ws.connected:
            return self
        ws_url = self._info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError(f"target {self.id} exposes no webSocketDebuggerUrl "
                           f"(type={self._info.get('type')!r})")
        self._ws = WebSocket(ws_url, timeout=self._chrome.timeout).connect()
        return self

    def detach(self) -> None:
        if self._ws is not None:
            self._ws.close()
        self._ws = None

    def __enter__(self):
        return self.attach()

    def __exit__(self, *_exc):
        self.detach()

    # -- protocol ----------------------------------------------------------
    def call(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        """Send one CDP command and return its result.

        CDP interleaves unsolicited events with responses on the same socket, so
        this reads until the id it sent comes back. A client that returns the
        first frame it sees will hand back a `Page.frameNavigated` event as if it
        were the result of the call.
        """
        if self._ws is None or not self._ws.connected:
            self.attach()
        assert self._ws is not None
        self._next_id += 1
        message_id = self._next_id
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send(json.dumps(payload))

        deadline = time.time() + (timeout or self._chrome.timeout)
        while True:
            if time.time() > deadline:
                raise CDPError(f"timed out waiting for {method}")
            try:
                raw = self._ws.recv()
            except WebSocketError as exc:
                raise CDPError(f"{method}: transport failed ({exc})") from exc
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if message.get("id") != message_id:
                continue  # an event, or a stale response
            if "error" in message:
                err = message["error"]
                raise CDPError(f"{method}: {err.get('code')} {err.get('message')}")
            return message.get("result", {})

    # -- ownership guard ---------------------------------------------------
    def _require_ownership(self, action: str, allow_foreign: bool) -> None:
        if self.owned or allow_foreign:
            return
        raise TabOwnershipError(
            f"refusing to {action} a tab this process did not create "
            f"({self.title[:50]!r}). Pass allow_foreign=True if that is intended."
        )

    # -- reading (always permitted) ---------------------------------------
    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            text = detail.get("exception", {}).get("description") or detail.get("text")
            raise CDPError(f"javascript threw: {text}")
        return result.get("result", {}).get("value")

    def html(self) -> str:
        document = self.call("DOM.getDocument", {"depth": -1})
        node_id = document.get("root", {}).get("nodeId")
        return self.call("DOM.getOuterHTML", {"nodeId": node_id}).get("outerHTML", "")

    def screenshot(self, path: str | None = None, image_format: str = "png") -> bytes:
        result = self.call("Page.captureScreenshot", {"format": image_format})
        data = base64.b64decode(result.get("data", ""))
        if path:
            with open(path, "wb") as handle:
                handle.write(data)
        return data

    def accessibility_tree(self) -> list[dict]:
        self.call("Accessibility.enable")
        return self.call("Accessibility.getFullAXTree").get("nodes", [])

    def cookies(self) -> list[dict]:
        return self.call("Network.getCookies").get("cookies", [])

    # -- mutation (ownership-guarded) -------------------------------------
    def navigate(self, url: str, wait: bool = True, timeout: float = 30.0,
                 allow_foreign: bool = False) -> "Tab":
        self._require_ownership(f"navigate to {url}", allow_foreign)
        self.call("Page.enable")
        self.call("Page.navigate", {"url": url})
        if wait:
            self.wait_for_load(timeout=timeout)
        self._info["url"] = url
        return self

    def wait_for_load(self, timeout: float = 30.0) -> bool:
        """Poll readyState rather than waiting on Page.loadEventFired.

        The load event may already have fired before this call, in which case an
        event wait blocks for the full timeout on a page that is finished. The
        poll answers correctly in both orderings.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return True
            except CDPError:
                pass
            time.sleep(0.05)
        return False

    def click(self, selector: str, allow_foreign: bool = False) -> bool:
        self._require_ownership(f"click {selector!r}", allow_foreign)
        return bool(self.evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" if (!el) return false; el.click(); return true; }})()"
        ))

    def type_text(self, selector: str, text: str, allow_foreign: bool = False) -> bool:
        self._require_ownership(f"type into {selector!r}", allow_foreign)
        return bool(self.evaluate(
            f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
            f" if (!el) return false; el.focus(); el.value = {json.dumps(text)};"
            f" el.dispatchEvent(new Event('input', {{bubbles:true}}));"
            f" el.dispatchEvent(new Event('change', {{bubbles:true}})); return true; }})()"
        ))


class Chrome:
    """A connection to a running Chrome's DevTools endpoint."""

    def __init__(self, port: int = DEFAULT_PORT, timeout: float = 30.0):
        self.port = port
        self.timeout = timeout
        self._owned_tab_ids: set[str] = set()

    # -- discovery ---------------------------------------------------------
    def version(self) -> dict:
        data = _http_json(self.port, "/json/version", timeout=self.timeout)
        return data if isinstance(data, dict) else {}

    def is_up(self) -> bool:
        try:
            self.version()
            return True
        except CDPError:
            return False

    def targets(self, kind: str | None = None) -> list[dict]:
        data = _http_json(self.port, "/json/list", timeout=self.timeout)
        items = data if isinstance(data, list) else []
        if kind:
            items = [t for t in items if t.get("type") == kind]
        return items

    def tabs(self) -> list[Tab]:
        """Attachable page targets, excluding DevTools' own windows."""
        return [
            Tab(self, info, owned=info.get("id") in self._owned_tab_ids)
            for info in self.targets("page")
            if info.get("webSocketDebuggerUrl")
            and not str(info.get("url", "")).startswith("devtools://")
        ]

    def find_tab(self, url_contains: str = "", title_contains: str = "") -> Tab | None:
        for tab in self.tabs():
            if url_contains and url_contains.lower() not in tab.url.lower():
                continue
            if title_contains and title_contains.lower() not in tab.title.lower():
                continue
            return tab
        return None

    # -- lifecycle ---------------------------------------------------------
    def new_tab(self, url: str = "about:blank") -> Tab:
        """Open a tab this process owns and may mutate freely."""
        try:
            info = _http_json(self.port, f"/json/new?{url}", method="PUT", timeout=self.timeout)
        except CDPError:
            # Chrome < 111 rejects PUT here; GET was the original spelling.
            info = _http_json(self.port, f"/json/new?{url}", method="GET", timeout=self.timeout)
        if not isinstance(info, dict) or not info.get("id"):
            raise CDPError(f"Chrome did not return a new target: {info!r}")
        self._owned_tab_ids.add(info["id"])
        return Tab(self, info, owned=True)

    def close_tab(self, tab: "Tab | str", allow_foreign: bool = False) -> bool:
        """Close a tab and CONFIRM it is gone by re-listing targets.

        The close endpoint answers `Target is closing`, which is a statement of
        intent, not of outcome. Confirmation comes from the target list.
        """
        target_id = tab if isinstance(tab, str) else tab.id
        if target_id not in self._owned_tab_ids and not allow_foreign:
            raise TabOwnershipError(
                f"refusing to close tab {target_id[:8]} that this process did not open"
            )
        if isinstance(tab, Tab):
            tab.detach()
        try:
            _http_json(self.port, f"/json/close/{target_id}", timeout=self.timeout)
        except CDPError:
            pass
        self._owned_tab_ids.discard(target_id)
        return not any(t.get("id") == target_id for t in self.targets())

    def close_all_owned(self) -> int:
        closed = 0
        for target_id in list(self._owned_tab_ids):
            if self.close_tab(target_id):
                closed += 1
        return closed

    def __enter__(self) -> "Chrome":
        return self

    def __exit__(self, *_exc) -> None:
        self.close_all_owned()

    def __repr__(self) -> str:
        return f"<Chrome 127.0.0.1:{self.port} owned_tabs={len(self._owned_tab_ids)}>"
