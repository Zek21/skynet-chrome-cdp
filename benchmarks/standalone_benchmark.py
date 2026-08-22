#!/usr/bin/env python3
"""Skynet CDP Benchmark -- a self-contained instrument for Chrome DevTools Protocol.

WHY THIS FILE HAS NO IMPORTS BEYOND THE STANDARD LIBRARY
--------------------------------------------------------
Two reasons, both load-bearing:

  1. PORTABILITY. This benchmark has to run on machines we do not administer --
     including a remote host reached through a command channel where `pip install`
     is not on the table. A single stdlib file can be transmitted as text and run.

  2. MEASUREMENT INDEPENDENCE. An instrument that imports the system under test
     measures the system *plus itself*. This file speaks raw RFC 6455 and raw CDP,
     so `--mode raw` establishes the SEQUENTIAL BASELINE: one request at a time,
     nothing else in flight. It is a measured reference point, NOT a theoretical
     minimum -- batching, pipelining and concurrent sessions are outside what it
     measures. Any connector library is then measured as overhead above that
     baseline, which is the only honest way to say a connector is "fast".

The baseline is the point. "0.5ms latency" is a claim; a p50 with a stated
sample count, a stated fixture, and a reproducible procedure is a measurement.

WHAT IT MEASURES
----------------
  rtt          Runtime.evaluate round-trip on a trivial expression. This is the
               sequential baseline -- one request, one response, no page work.
  dom          DOM.getDocument + DOM.getOuterHTML: bytes of raw HTML.
  ax           Accessibility.getFullAXTree: the structural perception input.
  actionable   AX nodes filtered to elements an agent can actually act on.
  screenshot   Page.captureScreenshot: latency and PNG bytes.

The `reduction` figure is the honest version of the "100k tokens -> 1.4k tokens"
claim: raw DOM characters divided by actionable-element characters, on a fixture
whose size we control and state.

FIXTURE
-------
`--fixture synthetic` (default) builds a deterministic DOM in an about:blank tab
this tool created itself: no network, no third-party page, identical on every
machine, so two hosts are comparable. `--fixture active` measures whatever tab is
already frontmost -- realistic, but NOT comparable across machines, and it is
reported as such.

SAFETY
------
Creates its own tab and closes it. Never navigates, mutates, or closes a tab it
did not create. If the repo's browser mutex is importable it is acquired; when it
is absent (standalone use) the tool still runs, because a benchmark that refuses
to run outside its home repo is not portable.

Usage:
    python tools/skynet_cdp_benchmark.py --port 9226 --samples 200
    python tools/skynet_cdp_benchmark.py --port 9226 --json --out results.json
    python tools/skynet_cdp_benchmark.py --port 9226 --fixture active

Exit codes: 0 ok, 2 benchmark could not run (no Chrome / no target), 3 usage.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import socket
import struct
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 2.0.0, not 1.1.0: the bare "ax_nodes" key was REMOVED, not deprecated. That
# breaks any consumer of the 1.x artifact, and a consumer that silently reads a
# missing key as zero is exactly the failure this change exists to prevent.
BENCHMARK_VERSION = "2.0.0"
DEFAULT_PORT = 9226
DEFAULT_SAMPLES = 200
WARMUP_SAMPLES = 20

# Elements the synthetic fixture contains. Stated so a reader can reproduce it.
FIXTURE_INTERACTIVE = 120
FIXTURE_TEXT_BLOCKS = 200

# The viewport the fixture is measured at. Pinned because part of the
# accessibility tree is LAYOUT-DERIVED: Chrome emits InlineTextBox nodes only
# when the active accessibility mode asks for them, and how many it emits tracks
# how the text is laid out. So the RAW node count reads the window and the
# browser's a11y mode, not the page.
#
# Measured 2026-08-22, Chrome 151.0.7922.140, identical synthetic fixture:
#   host Zeke        800px -> 1874 raw (630 InlineTextBox)
#   host Zeke  1280/1920/2560px -> 1684 raw (440 InlineTextBox)
#   host DESKTOP-OJ9K9QR, run 1 -> 1244 raw   (role breakdown NOT captured)
#   host DESKTOP-OJ9K9QR, run 2 -> 1684 raw (440 InlineTextBox), other instance
# Run 2's role histogram matches this host's on all 8 roles it reported
# (StaticText 440, generic 240, paragraph 200, LabelText 120, textbox 120,
# button 120, none 2) -- distribution identity, not structural identity; the
# parent/child edges were never compared.
#
# INFERRED, NOT MEASURED: that run 1's 1244 was InlineTextBox being absent. Width
# cannot produce it -- InlineTextBox floors at 440 here (one per StaticText) and
# does not move between 1280px and 2560px, so only an a11y-MODE difference gets
# to 1244. Note 1684-440 is also 1684-StaticText and 1684-(generic+paragraph):
# three disjoint role sets each total 440, so the arithmetic alone does not
# single out InlineTextBox. Run 1's tree was never enumerated by role.
BENCH_VIEWPORT = {"width": 1280, "height": 900, "deviceScaleFactor": 1,
                  "mobile": False}

# Nodes whose COUNT is a property of the host's layout/a11y mode rather than of
# the page. Excluded from the cross-machine node count.
LAYOUT_DERIVED_AX_ROLES = {"InlineTextBox"}

# Which measurements survive a move to another machine, and which do not. Stated
# in the artifact because a JSON file outlives the caveats told around it.
COMPARABLE_MEASUREMENTS = (
    "dom_chars", "ax_nodes_semantic", "actionable_nodes", "scene_chars",
    "reduction_ratio",
)
HOST_DEPENDENT_MEASUREMENTS = (
    "ax_nodes_raw", "ax_inline_text_boxes", "attach_ms", "evaluate_rtt",
    "dom_serialize", "ax_tree", "screenshot", "screenshot_bytes",
    "implied_calls_per_second_at_p50",
)
# Neither promised comparable nor known to vary: recorded to explain a tree, not
# to be compared. Listed explicitly because a field in NEITHER list reads as an
# oversight, and a reader will guess -- usually generously.
DIAGNOSTIC_MEASUREMENTS = ("ax_ignored_nodes", "scene_sample", "dom_chars_note")


# --------------------------------------------------------------------------
# Minimal RFC 6455 client. Enough for CDP: text frames, client masking,
# continuation frames, and payloads in the megabytes (screenshots).
# --------------------------------------------------------------------------
class WSError(Exception):
    pass


class MiniWebSocket:
    """A small, correct WebSocket client. No third-party packages."""

    def __init__(self, url, timeout=30.0):
        self.url = url
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def connect(self):
        if not self.url.startswith("ws://"):
            raise WSError(f"only ws:// is supported, got {self.url!r}")
        rest = self.url[len("ws://"):]
        netloc, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_s = netloc.partition(":")
        port = int(port_s or 80)

        self._sock = socket.create_connection((host, port), timeout=self.timeout)
        self._sock.settimeout(self.timeout)
        # Nagle off: CDP is a request/response chat of small frames. Leaving Nagle
        # on adds up to ~40ms of coalescing delay per call and would make every
        # latency number in this file a measurement of Nagle, not of Chrome.
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(req.encode())

        # Read headers up to the blank line.
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WSError("connection closed during handshake")
            head += chunk
        header_blob, _, remainder = head.partition(b"\r\n\r\n")
        status = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in status:
            raise WSError(f"handshake refused: {status}")
        self._buf = remainder
        return self

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray()
        header.append(0x81)  # FIN + text
        mask_bit = 0x80
        n = len(payload)
        if n < 126:
            header.append(mask_bit | n)
        elif n < (1 << 16):
            header.append(mask_bit | 126)
            header += struct.pack(">H", n)
        else:
            header.append(mask_bit | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def recv(self):
        """Return one complete application message as str, reassembling fragments."""
        chunks = []
        while True:
            b0, b1 = self._recv_exact(2)
            fin = b0 & 0x80
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # close
                raise WSError("server closed the websocket")
            if opcode == 0x9:  # ping -> pong
                self._sock.sendall(b"\x8a\x80" + os.urandom(4))
                continue
            if opcode == 0xA:  # pong
                continue

            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self):
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        self._sock = None


# --------------------------------------------------------------------------
# Minimal CDP session
# --------------------------------------------------------------------------
class MiniCDP:
    def __init__(self, ws_url, timeout=30.0):
        self.ws = MiniWebSocket(ws_url, timeout=timeout)
        self._id = 0

    def connect(self):
        self.ws.connect()
        return self

    def call(self, method, params=None):
        self._id += 1
        mid = self._id
        msg = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        self.ws.send(json.dumps(msg))
        # CDP interleaves events with responses; skip until our id comes back.
        while True:
            raw = self.ws.recv()
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if data.get("id") == mid:
                if "error" in data:
                    err = data["error"]
                    raise WSError(f"CDP {method}: {err.get('code')} {err.get('message')}")
                return data.get("result", {})

    def close(self):
        self.ws.close()


def http_json(port, path, method="GET", timeout=8.0):
    url = f"http://127.0.0.1:{port}{path}"
    req = Request(url, method=method)
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if not body.strip():
        return {}
    return json.loads(body)


def http_text(port, path, method="GET", timeout=8.0):
    """Some CDP HTTP endpoints answer in plain text, not JSON.

    `/json/close/<id>` replies with the bare string `Target is closing`. Parsing
    that as JSON raises, and an earlier version of this file turned that raise
    into "could not close own tab" -- while the tab had in fact closed. A false
    hygiene warning in a published benchmark is a bug, so this path never parses.
    """
    url = f"http://127.0.0.1:{port}{path}"
    req = Request(url, method=method)
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def target_exists(port, target_id, timeout=8.0):
    """Independent confirmation, rather than trusting a close response body."""
    try:
        targets = http_json(port, "/json/list", timeout=timeout)
    except Exception:
        return None  # unknown, which is not the same as "still open"
    return any(t.get("id") == target_id for t in targets)


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def percentile(sorted_values, pct):
    """Nearest-rank percentile. Stated explicitly so results are reproducible."""
    if not sorted_values:
        return None
    k = max(1, int(round(pct / 100.0 * len(sorted_values))))
    return sorted_values[min(k, len(sorted_values)) - 1]


def summarize(samples_ms):
    if not samples_ms:
        return {"n": 0}
    s = sorted(samples_ms)
    n = len(s)
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n if n > 1 else 0.0
    return {
        "n": n,
        "min_ms": round(s[0], 4),
        "p50_ms": round(percentile(s, 50), 4),
        "p90_ms": round(percentile(s, 90), 4),
        "p99_ms": round(percentile(s, 99), 4),
        "max_ms": round(s[-1], 4),
        "mean_ms": round(mean, 4),
        "stdev_ms": round(var ** 0.5, 4),
    }


# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------
FIXTURE_JS = """
(() => {
  const INTERACTIVE = %d, TEXT = %d;
  document.title = 'skynet-cdp-benchmark-fixture';
  const root = document.createElement('main');
  root.id = 'skynet-fixture';
  for (let i = 0; i < TEXT; i++) {
    const p = document.createElement('p');
    p.className = 'copy block-' + i;
    p.setAttribute('data-index', String(i));
    p.textContent = 'Paragraph ' + i + ' of deterministic benchmark copy used to give '
      + 'the serializer a realistic amount of inert text to walk through.';
    root.appendChild(p);
  }
  for (let i = 0; i < INTERACTIVE; i++) {
    const wrap = document.createElement('div');
    wrap.className = 'field-wrap idx-' + i;
    const label = document.createElement('label');
    label.setAttribute('for', 'input-' + i);
    label.textContent = 'Field ' + i;
    const input = document.createElement('input');
    input.id = 'input-' + i;
    input.name = 'field_' + i;
    input.type = 'text';
    input.setAttribute('aria-label', 'Field ' + i);
    const btn = document.createElement('button');
    btn.id = 'btn-' + i;
    btn.textContent = 'Submit ' + i;
    wrap.appendChild(label); wrap.appendChild(input); wrap.appendChild(btn);
    root.appendChild(wrap);
  }
  document.body.innerHTML = '';
  document.body.appendChild(root);
  return {
    interactive: INTERACTIVE,
    text_blocks: TEXT,
    elements: document.getElementsByTagName('*').length
  };
})()
""" % (FIXTURE_INTERACTIVE, FIXTURE_TEXT_BLOCKS)

ACTIONABLE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox", "listbox",
    "menuitem", "tab", "switch", "slider", "searchbox", "option",
}


def eval_value(cdp, expression, await_promise=False):
    res = cdp.call("Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })
    return res.get("result", {}).get("value")


# --------------------------------------------------------------------------
# Benchmark
# --------------------------------------------------------------------------
def run_benchmark(port, samples, fixture_mode, timeout):
    started = time.time()
    report = {
        "benchmark_version": BENCHMARK_VERSION,
        "mode": "raw",
        "fixture": fixture_mode,
        "port": port,
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "processor": platform.processor() or "unknown",
            "cpu_count": os.cpu_count(),
            "node": platform.node(),
        },
        # Set honestly AFTER the viewport is pinned. A synthetic fixture read at
        # whatever size this window happens to be is NOT comparable: see
        # BENCH_VIEWPORT for the two machines that proved it.
        "comparable_across_machines": False,
        "comparable_measurements": list(COMPARABLE_MEASUREMENTS),
        "host_dependent_measurements": list(HOST_DEPENDENT_MEASUREMENTS),
        "diagnostic_measurements": list(DIAGNOSTIC_MEASUREMENTS),
        "viewport": None,
        "measurements": {},
        "notes": [],
    }

    try:
        version = http_json(port, "/json/version", timeout=timeout)
    except (URLError, OSError, ValueError) as exc:
        return None, f"no CDP endpoint on 127.0.0.1:{port} ({exc})"

    report["browser"] = {
        "product": version.get("Browser", "unknown"),
        "protocol_version": version.get("Protocol-Version", "unknown"),
        "v8": version.get("V8-Version", "unknown"),
    }

    owned_tab = None
    try:
        if fixture_mode == "synthetic":
            # PUT is required by current Chrome; older builds accept GET.
            try:
                tab = http_json(port, "/json/new?about:blank", method="PUT", timeout=timeout)
            except Exception:
                tab = http_json(port, "/json/new?about:blank", method="GET", timeout=timeout)
            owned_tab = tab.get("id")
            ws_url = tab.get("webSocketDebuggerUrl")
            if not ws_url:
                return None, "Chrome did not return a webSocketDebuggerUrl for the new tab"
        else:
            targets = http_json(port, "/json/list", timeout=timeout)
            pages = [t for t in targets
                     if t.get("type") == "page"
                     and t.get("webSocketDebuggerUrl")
                     and not str(t.get("url", "")).startswith("devtools://")]
            if not pages:
                return None, "no attachable page target found"
            ws_url = pages[0]["webSocketDebuggerUrl"]
            report["notes"].append(f"measured existing tab: {pages[0].get('url','')[:80]}")

        cdp = MiniCDP(ws_url, timeout=timeout).connect()
    except (URLError, OSError, ValueError, WSError) as exc:
        return None, f"could not attach: {exc}"

    viewport_pinned = False  # bound here so the finally can always read it
    try:
        connect_ms = (time.time() - started) * 1000.0
        report["measurements"]["attach_ms"] = round(connect_ms, 3)

        cdp.call("Runtime.enable")
        cdp.call("Page.enable")

        # Pin the viewport BEFORE measuring anything. Unpinned, the accessibility
        # node count is a reading of THIS window's width rather than of the page,
        # and two honest machines report numbers hundreds of nodes apart.
        # ONLY on a tab this process created. In --fixture active we are attached
        # to a tab somebody else owns; resizing it would mutate a window the
        # operator is using, which the connector's tab-ownership rule forbids.
        viewport_pinned = False
        if not owned_tab:
            report["viewport"] = {
                "requested": dict(BENCH_VIEWPORT),
                "measured_inner": eval_value(cdp, "[innerWidth, innerHeight]"),
                "pinned": False,
                "why": "not our tab: refused to resize a window we do not own",
            }
            report["notes"].append(
                "viewport left alone because this run measured a tab it does not "
                "own; node counts are host-dependent and not cross-machine data")
        else:
            try:
                cdp.call("Emulation.setDeviceMetricsOverride", dict(BENCH_VIEWPORT))
                time.sleep(0.35)  # a tree read mid-relayout is a guess, not a reading
                inner = eval_value(cdp, "[innerWidth, innerHeight]")
                viewport_pinned = (isinstance(inner, (list, tuple)) and len(inner) == 2
                                   and inner[0] == BENCH_VIEWPORT["width"])
                report["viewport"] = {"requested": dict(BENCH_VIEWPORT),
                                      "measured_inner": inner,
                                      "pinned": viewport_pinned}
                if not viewport_pinned:
                    report["notes"].append(
                        f"viewport override did not take: asked for "
                        f"{BENCH_VIEWPORT['width']}px, window reports {inner}")
            except (WSError, OSError) as exc:
                report["viewport"] = {"requested": dict(BENCH_VIEWPORT),
                                      "measured_inner": None, "pinned": False,
                                      "error": str(exc)}
                report["notes"].append(
                    f"viewport could not be pinned ({exc}): node counts on this run "
                    f"are host-dependent and not comparable to another machine")

        # Comparability is a property of the fixture AND of the conditions it was
        # measured under. Both must hold.
        report["comparable_across_machines"] = (
            fixture_mode == "synthetic" and viewport_pinned)

        if fixture_mode == "synthetic":
            built = eval_value(cdp, FIXTURE_JS)
            if not isinstance(built, dict):
                return None, "fixture did not build"
            report["fixture_detail"] = built

        # --- 1. Sequential baseline: Runtime.evaluate round-trip --------
        for _ in range(WARMUP_SAMPLES):
            eval_value(cdp, "1+1")
        rtt = []
        for _ in range(samples):
            t0 = time.perf_counter()
            eval_value(cdp, "1+1")
            rtt.append((time.perf_counter() - t0) * 1000.0)
        report["measurements"]["evaluate_rtt"] = summarize(rtt)

        # --- 2. DOM serialization ---------------------------------------
        dom_ms, dom_bytes = [], 0
        for i in range(min(20, samples)):
            t0 = time.perf_counter()
            doc = cdp.call("DOM.getDocument", {"depth": -1})
            node_id = doc.get("root", {}).get("nodeId")
            html = cdp.call("DOM.getOuterHTML", {"nodeId": node_id}).get("outerHTML", "")
            dom_ms.append((time.perf_counter() - t0) * 1000.0)
            dom_bytes = len(html)
        report["measurements"]["dom_serialize"] = summarize(dom_ms)
        report["measurements"]["dom_chars"] = dom_bytes

        # --- 3. Accessibility tree (structural perception input) --------
        ax_ms, ax_raw, ax_inline, ax_ignored = [], 0, 0, 0
        actionable, scene_lines = 0, []
        try:
            cdp.call("Accessibility.enable")
            for _ in range(min(20, samples)):
                t0 = time.perf_counter()
                ax = cdp.call("Accessibility.getFullAXTree")
                ax_ms.append((time.perf_counter() - t0) * 1000.0)
                nodes = ax.get("nodes", [])
                ax_raw = len(nodes)
                ax_inline, ax_ignored = 0, 0
                scene_lines = []
                for n in nodes:
                    if str(n.get("role", {}).get("value", "")) in LAYOUT_DERIVED_AX_ROLES:
                        ax_inline += 1
                    if n.get("ignored", False):
                        ax_ignored += 1
                        continue
                    role = str(n.get("role", {}).get("value", "")).lower()
                    if role not in ACTIONABLE_ROLES:
                        continue
                    name = str(n.get("name", {}).get("value", ""))[:80]
                    scene_lines.append(f'{role} "{name}"')
                actionable = len(scene_lines)
            report["measurements"]["ax_tree"] = summarize(ax_ms)
            # Deliberately NOT reported as a bare "ax_nodes". An unqualified node
            # count is what invited two machines to be compared on a number that
            # measures their window and their a11y mode, not the connector.
            report["measurements"]["ax_nodes_raw"] = ax_raw
            report["measurements"]["ax_inline_text_boxes"] = ax_inline
            report["measurements"]["ax_nodes_semantic"] = ax_raw - ax_inline
            report["measurements"]["ax_ignored_nodes"] = ax_ignored
            report["measurements"]["actionable_nodes"] = actionable
            report["notes"].append(
                "ax_nodes_semantic = raw tree minus InlineTextBox nodes. Chrome "
                "emits InlineTextBox nodes only when the active accessibility "
                "mode asks for them, and how many it emits tracks how the text is "
                "laid out, so ax_nodes_raw reads this window and this browser's "
                "a11y mode as much as it reads the page. Two hosts differed by 440 "
                "here while agreeing on dom_chars, actionable_nodes, scene_chars "
                "and reduction_ratio. They did NOT agree on screenshot_bytes "
                "(119683 vs 109932): they painted different pixels, which is the "
                "same layout difference seen from another angle."
            )
        except WSError as exc:
            report["notes"].append(f"accessibility domain unavailable: {exc}")

        # --- 4. Perception reduction ------------------------------------
        # The reduction claim is only meaningful if the "after" side is a real
        # string. So build the actual scene an agent would be handed -- one line
        # per actionable element -- and measure its length. No assumed line width.
        if scene_lines and dom_bytes:
            scene_text = "\n".join(scene_lines)
            report["measurements"]["scene_chars"] = len(scene_text)
            report["measurements"]["reduction_ratio"] = round(dom_bytes / len(scene_text), 2)
            report["measurements"]["scene_sample"] = scene_lines[:3]
            report["notes"].append(
                "reduction_ratio = raw DOM chars / measured scene chars, where the "
                "scene is one 'role \"name\"' line per actionable node. The ratio is "
                "a property of the FIXTURE as much as the connector: a form-dense "
                "page has many actionable nodes and reduces less than a content page."
            )

        # --- 5. Screenshot ----------------------------------------------
        shot_ms, shot_bytes = [], 0
        for _ in range(min(10, samples)):
            t0 = time.perf_counter()
            res = cdp.call("Page.captureScreenshot", {"format": "png"})
            shot_ms.append((time.perf_counter() - t0) * 1000.0)
            shot_bytes = len(base64.b64decode(res.get("data", "")))
        report["measurements"]["screenshot"] = summarize(shot_ms)
        report["measurements"]["screenshot_bytes"] = shot_bytes

        # --- 6. Derived throughput --------------------------------------
        p50 = report["measurements"]["evaluate_rtt"].get("p50_ms")
        if p50:
            # DERIVED from p50, not a measured throughput: it is the rate implied
            # by the median sequential round trip, with no concurrency involved.
            report["measurements"]["implied_calls_per_second_at_p50"] = round(1000.0 / p50, 1)

        report["duration_s"] = round(time.time() - started, 2)
        report["ok"] = True
        return report, None

    finally:
        # Undo the emulation before letting go, so a tab that outlives a failed
        # close is not left stuck at an overridden size.
        if viewport_pinned:
            try:
                cdp.call("Emulation.clearDeviceMetricsOverride")
            except Exception:
                pass
        try:
            cdp.close()
        except Exception:
            pass
        if owned_tab:
            try:
                http_text(port, f"/json/close/{owned_tab}", timeout=timeout)
            except Exception:
                pass
            # Trust the re-probe, not the response body.
            still_open = target_exists(port, owned_tab, timeout=timeout)
            report["own_tab_closed"] = (still_open is False)
            if still_open is True:
                report["notes"].append(
                    f"LEFT A TAB OPEN: {owned_tab} -- close it manually")
            elif still_open is None:
                report["notes"].append(
                    f"could not confirm own tab {owned_tab} closed (probe failed)")


def render_human(report):
    m = report["measurements"]
    out = []
    out.append("SKYNET CDP BENCHMARK  v" + report["benchmark_version"])
    out.append("=" * 62)
    b = report.get("browser", {})
    out.append(f"browser      : {b.get('product')}  (CDP {b.get('protocol_version')})")
    out.append(f"host         : {report['host']['node']}  {report['host']['platform']}")
    out.append(f"python       : {report['host']['python']}   cpus: {report['host']['cpu_count']}")
    out.append(f"fixture      : {report['fixture']}"
               f"   comparable_across_machines={report['comparable_across_machines']}")
    vp = report.get("viewport") or {}
    out.append(f"viewport     : {vp.get('measured_inner')} pinned={vp.get('pinned')}"
               f"   (node counts are meaningless without this)")
    if report.get("fixture_detail"):
        fd = report["fixture_detail"]
        out.append(f"               {fd.get('elements')} elements "
                   f"({fd.get('interactive')} interactive, {fd.get('text_blocks')} text)")
    out.append("-" * 62)
    rtt = m.get("evaluate_rtt", {})
    out.append(f"attach            : {m.get('attach_ms')} ms (one-time)")
    out.append(f"evaluate RTT      : p50 {rtt.get('p50_ms')} ms | p90 {rtt.get('p90_ms')} ms "
               f"| p99 {rtt.get('p99_ms')} ms  (n={rtt.get('n')})")
    out.append(f"                    min {rtt.get('min_ms')} | max {rtt.get('max_ms')} "
               f"| stdev {rtt.get('stdev_ms')}")
    out.append(f"implied rate      : {m.get('implied_calls_per_second_at_p50')} calls/s "
               f"(derived 1000/p50, not measured throughput)")
    ds = m.get("dom_serialize", {})
    out.append(f"DOM serialize     : p50 {ds.get('p50_ms')} ms -> {m.get('dom_chars')} chars")
    ax = m.get("ax_tree", {})
    if ax:
        out.append(f"A11y full tree    : p50 {ax.get('p50_ms')} ms -> "
                   f"{m.get('ax_nodes_semantic')} semantic nodes, "
                   f"{m.get('actionable_nodes')} actionable")
        out.append(f"                    raw {m.get('ax_nodes_raw')} incl. "
                   f"{m.get('ax_inline_text_boxes')} InlineTextBox "
                   f"(host-dependent, not comparable)")
    if m.get("reduction_ratio"):
        out.append(f"perception gain   : {m.get('reduction_ratio')}x smaller than raw DOM "
                   f"({m.get('dom_chars')} -> {m.get('scene_chars')} chars measured)")
    ss = m.get("screenshot", {})
    out.append(f"screenshot        : p50 {ss.get('p50_ms')} ms -> {m.get('screenshot_bytes')} bytes")
    out.append("-" * 62)
    for n in report.get("notes", []):
        out.append(f"note: {n}")
    # Printed because it was NOT printed. A reviewer running this on another
    # machine reported that the human output computes own_tab_closed and then
    # hides it, so they could not honestly state whether the benchmark had
    # cleaned up after itself -- and correctly declined to claim it had. A
    # hygiene field that only appears under --json is a hygiene field nobody
    # checks.
    closed = report.get("own_tab_closed")
    if closed is not None:
        out.append(f"own tab closed   : {closed}")
    out.append(f"duration: {report.get('duration_s')}s")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Self-contained CDP performance benchmark")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--fixture", choices=["synthetic", "active"], default="synthetic")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--out", help="write the JSON report here")
    ap.add_argument("--label", help="free-text label recorded in the report")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    if args.samples < 1:
        print("--samples must be >= 1", file=sys.stderr)
        return 3

    # Politeness, not a hard dependency: if the repo's browser mutex is present,
    # take it so we do not interleave with another Skynet driver.
    lock = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from cdp_browser_lock import browser_lock  # type: ignore
        lock = browser_lock("skynet_cdp_benchmark")
        lock.__enter__()
    except Exception:
        lock = None

    try:
        report, err = run_benchmark(args.port, args.samples, args.fixture, args.timeout)
    finally:
        if lock is not None:
            try:
                lock.__exit__(None, None, None)
            except Exception:
                pass

    if report is None:
        payload = {"ok": False, "error": err, "port": args.port}
        print(json.dumps(payload, indent=2) if args.json else f"BENCHMARK FAILED: {err}",
              file=sys.stderr)
        return 2

    if args.label:
        report["label"] = args.label

    if args.out:
        out_path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        report["written_to"] = out_path

    print(json.dumps(report, indent=2) if args.json else render_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
