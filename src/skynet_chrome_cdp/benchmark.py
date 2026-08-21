"""Reproducible performance measurement for a CDP connector.

MEASUREMENT POSITION
--------------------
Browser automation projects quote latency figures that cannot be reproduced: no
fixture, no sample count, no percentile, no machine. This module publishes the
procedure alongside the numbers so a reader can re-run it and disagree.

Three rules it follows:

  1. STATE THE FIXTURE. The default fixture is built by this file inside a blank
     tab it opened itself: a fixed number of interactive controls and text blocks,
     no network, identical on every machine. Two hosts running `--fixture
     synthetic` are comparing the same work. `--fixture active` measures whatever
     page is open, which is realistic but NOT comparable, and the report says so
     in a machine-readable field rather than a footnote.

  2. REPORT DISTRIBUTIONS. A mean hides the tail that actually breaks automation.
     Percentiles are nearest-rank, stated here so the arithmetic is auditable.

  3. SEPARATE THE BASELINE FROM THE LIBRARY. `Runtime.evaluate` on a trivial
     expression is the SEQUENTIAL BASELINE: one request, one response, no page
     work, nothing else in flight. It is a measured reference point, not a
     theoretical minimum -- batching and concurrency are not measured here.
     Everything else in this report should be read as work done above it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import sys
import time

from .cdp import CDPError, Chrome
from .perception import scene

__all__ = ["run", "summarize", "percentile", "FIXTURE_INTERACTIVE", "FIXTURE_TEXT_BLOCKS"]

BENCHMARK_VERSION = "1.0.0"
WARMUP_SAMPLES = 20

FIXTURE_INTERACTIVE = 120
FIXTURE_TEXT_BLOCKS = 200

FIXTURE_JS = """
(() => {
  const INTERACTIVE = %d, TEXT = %d;
  document.title = 'skynet-cdp-benchmark-fixture';
  const root = document.createElement('main');
  root.id = 'skynet-fixture';
  // Controls and prose are INTERLEAVED rather than stacked in two blocks. When
  // all the text came first, every control sat below the fold, the viewport
  // hit-test correctly rejected all of them, and the run reported a page with
  // nothing on it. Interleaving matches how real documents are laid out and
  // keeps some controls above the fold on any ordinary viewport.
  const ratio = Math.max(1, Math.round(TEXT / INTERACTIVE));
  let textWritten = 0;
  for (let i = 0; i < INTERACTIVE; i++) {
    const wrap = document.createElement('div');
    wrap.className = 'field-wrap idx-' + i;
    const label = document.createElement('label');
    label.setAttribute('for', 'input-' + i);
    label.textContent = 'Field ' + i;
    const input = document.createElement('input');
    input.id = 'input-' + i; input.name = 'field_' + i; input.type = 'text';
    input.setAttribute('aria-label', 'Field ' + i);
    const btn = document.createElement('button');
    btn.id = 'btn-' + i; btn.textContent = 'Submit ' + i;
    wrap.appendChild(label); wrap.appendChild(input); wrap.appendChild(btn);
    root.appendChild(wrap);
    for (let k = 0; k < ratio && textWritten < TEXT; k++, textWritten++) {
      const p = document.createElement('p');
      p.className = 'copy block-' + textWritten;
      p.setAttribute('data-index', String(textWritten));
      p.textContent = 'Paragraph ' + textWritten + ' of deterministic benchmark copy '
        + 'used to give the serializer a realistic amount of inert text to walk through.';
      root.appendChild(p);
    }
  }
  while (textWritten < TEXT) {
    const p = document.createElement('p');
    p.className = 'copy block-' + textWritten;
    p.setAttribute('data-index', String(textWritten));
    p.textContent = 'Paragraph ' + textWritten + ' of deterministic benchmark copy '
      + 'used to give the serializer a realistic amount of inert text to walk through.';
    root.appendChild(p); textWritten++;
  }
  document.body.innerHTML = '';
  document.body.appendChild(root);
  return {interactive: INTERACTIVE, text_blocks: TEXT,
          elements: document.getElementsByTagName('*').length};
})()
""" % (FIXTURE_INTERACTIVE, FIXTURE_TEXT_BLOCKS)


def percentile(sorted_values: list[float], pct: float):
    """Nearest-rank percentile: the smallest value at or above the pct-th rank."""
    if not sorted_values:
        return None
    rank = max(1, int(round(pct / 100.0 * len(sorted_values))))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summarize(samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {"n": 0}
    ordered = sorted(samples_ms)
    n = len(ordered)
    mean = sum(ordered) / n
    variance = sum((x - mean) ** 2 for x in ordered) / n if n > 1 else 0.0
    return {
        "n": n,
        "min_ms": round(ordered[0], 4),
        "p50_ms": round(percentile(ordered, 50), 4),
        "p90_ms": round(percentile(ordered, 90), 4),
        "p99_ms": round(percentile(ordered, 99), 4),
        "max_ms": round(ordered[-1], 4),
        "mean_ms": round(mean, 4),
        "stdev_ms": round(variance ** 0.5, 4),
    }


def run(port: int = 9222, samples: int = 200, fixture: str = "synthetic",
        timeout: float = 30.0, label: str | None = None) -> dict:
    """Execute the benchmark and return the report. Raises CDPError if it cannot."""
    started = time.time()
    report: dict = {
        "benchmark_version": BENCHMARK_VERSION,
        "connector_version": __import__("skynet_chrome_cdp").__version__,
        "fixture": fixture,
        "comparable_across_machines": fixture == "synthetic",
        "port": port,
        "label": label or "",
        "host": {
            "node": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "measurements": {},
        "notes": [],
    }

    chrome = Chrome(port=port, timeout=timeout)
    version_info = chrome.version()
    report["browser"] = {
        "product": version_info.get("Browser", "unknown"),
        "protocol_version": version_info.get("Protocol-Version", "unknown"),
        "v8": version_info.get("V8-Version", "unknown"),
    }

    owned = None
    try:
        attach_started = time.time()
        if fixture == "synthetic":
            tab = chrome.new_tab("about:blank")
            owned = tab
            tab.attach()
        else:
            tabs = chrome.tabs()
            if not tabs:
                raise CDPError("no attachable page target; open a tab first")
            tab = tabs[0].attach()
            report["notes"].append(f"measured an existing tab: {tab.url[:80]}")
        report["measurements"]["attach_ms"] = round((time.time() - attach_started) * 1000, 3)

        if fixture == "synthetic":
            built = tab.evaluate(FIXTURE_JS)
            report["fixture_detail"] = built

        # 1. Sequential baseline
        for _ in range(WARMUP_SAMPLES):
            tab.evaluate("1+1")
        rtt: list[float] = []
        for _ in range(samples):
            t0 = time.perf_counter()
            tab.evaluate("1+1")
            rtt.append((time.perf_counter() - t0) * 1000.0)
        report["measurements"]["evaluate_rtt"] = summarize(rtt)
        p50 = report["measurements"]["evaluate_rtt"]["p50_ms"]
        if p50:
            # DERIVED from p50, not measured throughput: the rate implied by the
            # median sequential round trip, with no concurrency involved.
            report["measurements"]["implied_calls_per_second_at_p50"] = round(1000.0 / p50, 1)

        # 2. Raw DOM serialisation
        dom_ms: list[float] = []
        dom_chars = 0
        for _ in range(min(20, samples)):
            t0 = time.perf_counter()
            html = tab.html()
            dom_ms.append((time.perf_counter() - t0) * 1000.0)
            dom_chars = len(html)
        report["measurements"]["dom_serialize"] = summarize(dom_ms)
        report["measurements"]["dom_chars"] = dom_chars

        # 3. Structural perception
        perception_ms: list[float] = []
        page_scene = None
        for _ in range(min(20, samples)):
            t0 = time.perf_counter()
            page_scene = scene(tab)
            perception_ms.append((time.perf_counter() - t0) * 1000.0)
        report["measurements"]["perception"] = summarize(perception_ms)
        if page_scene is not None:
            report["measurements"].update({
                "candidate_elements": len(page_scene.elements),
                "reachable_elements": len(page_scene.reachable),
                "onscreen_elements": len(page_scene.actionable),
                "scene_chars": page_scene.scene_chars,
                "reduction_ratio": page_scene.reduction_ratio,
                "scene_degenerate": page_scene.degenerate,
                "scene_sample": [e.to_line() for e in page_scene.reachable[:3]],
            })
            report["notes"].append(
                "reduction_ratio = measured DOM chars / measured scene chars for THIS "
                "page. It is a property of the page as much as of the connector: a "
                "form-dense fixture reduces far less than an article."
            )
            if page_scene.degenerate:
                report["notes"].append(
                    "SCENE WAS DEGENERATE: no elements perceived on a non-empty page. "
                    "reduction_ratio is null rather than a large number, because "
                    "dividing by an empty scene measures the failure, not the gain."
                )

        # 4. Accessibility tree, for comparison with perception
        ax_ms: list[float] = []
        for _ in range(min(20, samples)):
            t0 = time.perf_counter()
            nodes = tab.accessibility_tree()
            ax_ms.append((time.perf_counter() - t0) * 1000.0)
        report["measurements"]["accessibility_tree"] = summarize(ax_ms)
        report["measurements"]["ax_nodes"] = len(nodes)

        # 5. Screenshot, the alternative perception channel
        shot_ms: list[float] = []
        shot_bytes = 0
        for _ in range(min(10, samples)):
            t0 = time.perf_counter()
            data = tab.screenshot()
            shot_ms.append((time.perf_counter() - t0) * 1000.0)
            shot_bytes = len(data)
        report["measurements"]["screenshot"] = summarize(shot_ms)
        report["measurements"]["screenshot_bytes"] = shot_bytes

        report["duration_s"] = round(time.time() - started, 2)
        report["ok"] = True
        return report
    finally:
        if owned is not None:
            owned.detach()
            report["own_tab_closed"] = chrome.close_tab(owned)
            if not report["own_tab_closed"]:
                report["notes"].append(f"LEFT A TAB OPEN: {owned.id}")


def render(report: dict) -> str:
    m = report["measurements"]
    lines = [
        f"SKYNET CHROME CDP BENCHMARK  connector {report.get('connector_version')} "
        f"/ harness {report['benchmark_version']}",
        "=" * 68,
        f"browser   : {report['browser']['product']} (CDP {report['browser']['protocol_version']})",
        f"host      : {report['host']['node']}  {report['host']['platform']}",
        f"python    : {report['host']['python']}   cpus: {report['host']['cpu_count']}",
        f"fixture   : {report['fixture']}  "
        f"comparable_across_machines={report['comparable_across_machines']}",
    ]
    if report.get("fixture_detail"):
        detail = report["fixture_detail"]
        lines.append(f"            {detail.get('elements')} elements "
                     f"({detail.get('interactive')} interactive, "
                     f"{detail.get('text_blocks')} text blocks)")
    rtt = m.get("evaluate_rtt", {})
    lines += [
        "-" * 68,
        f"attach            : {m.get('attach_ms')} ms (one-time)",
        f"evaluate RTT      : p50 {rtt.get('p50_ms')} | p90 {rtt.get('p90_ms')} | "
        f"p99 {rtt.get('p99_ms')} ms   (n={rtt.get('n')})",
        f"implied rate      : {m.get('implied_calls_per_second_at_p50')} calls/s "
        f"(derived 1000/p50, not measured throughput)",
        f"DOM serialize     : p50 {m.get('dom_serialize', {}).get('p50_ms')} ms "
        f"-> {m.get('dom_chars')} chars",
        f"perception scene  : p50 {m.get('perception', {}).get('p50_ms')} ms "
        f"-> {m.get('scene_chars')} chars",
        f"                    {m.get('reachable_elements')} reachable "
        f"({m.get('onscreen_elements')} on screen) of "
        f"{m.get('candidate_elements')} candidates",
        f"reduction         : " + ("n/a (degenerate scene)"
                                   if m.get("reduction_ratio") is None
                                   else f"{m.get('reduction_ratio')}x on this fixture"),
        f"a11y full tree    : p50 {m.get('accessibility_tree', {}).get('p50_ms')} ms "
        f"-> {m.get('ax_nodes')} nodes",
        f"screenshot        : p50 {m.get('screenshot', {}).get('p50_ms')} ms "
        f"-> {m.get('screenshot_bytes')} bytes",
        "-" * 68,
    ]
    for note in report.get("notes", []):
        lines.append(f"note: {note}")
    lines.append(f"own tab closed: {report.get('own_tab_closed')}   "
                 f"duration: {report.get('duration_s')}s")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m skynet_chrome_cdp.benchmark",
                                     description="Reproducible CDP performance benchmark")
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--fixture", choices=["synthetic", "active"], default="synthetic")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", help="write the JSON report to this path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    try:
        report = run(port=args.port, samples=args.samples, fixture=args.fixture,
                     timeout=args.timeout, label=args.label)
    except CDPError as exc:
        print(f"benchmark could not run: {exc}", file=sys.stderr)
        return 2

    if args.out:
        path = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        report["written_to"] = path

    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
