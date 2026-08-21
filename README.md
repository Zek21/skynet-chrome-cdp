# Skynet Chrome CDP

**Drive the Chrome you are already signed into. No dependencies.**

This connector attaches to a browser that is already running and already signed
in — its cookies, its sessions, its extensions, its profile — and treats that as
the only mode of operation.

To be precise, because the easy version of this claim is false: Playwright and
Puppeteer **can** also attach to a running browser, via
`chromium.connectOverCDP()` and `puppeteer.connect({browserURL})`. The difference
is not capability, it is what each design treats as the normal case. There,
attaching is an escape hatch on a launch-first tool. Here it is the premise, which
changes the defaults that actually matter — see [tab ownership](#why-this-exists)
below.

```python
from skynet_chrome_cdp import Chrome, scene

with Chrome(port=9222) as chrome:
    tab = chrome.new_tab("https://example.com")   # a tab we own
    with tab:
        print(scene(tab).to_text())               # what can be acted on
```

```
Example Domain — https://example.com
link "More information..." @420,310
```

---

## Why this exists

Three properties, none of which are available together elsewhere:

**1. It attaches to a real session.** Automated sign-in is the most fragile part
of most automation: MFA, device checks, and bot detection all target it. A
session-attached connector does not sign in. It uses the session a human already
established.

**2. It has no dependencies.** Not "few" — none. The WebSocket layer is ~200
lines of `socket` and `struct`.

That is a constraint this project accepts, not a claim about what everyone else
does. The common Python CDP clients pull in `websocket-client` or `websockets`,
which is a perfectly reasonable choice — until the machine you need to automate
is a locked-down corporate image, a container built from a lockfile, or a host
you reach only through a command channel. This library can be copied as a single
file and run.

**3. It refuses to touch tabs it does not own.** The browser it drives contains
the user's real sessions. Navigating "the current tab" can mean navigating away
from someone's half-written message. Mutating a foreign tab raises
`TabOwnershipError` unless you explicitly opt in per call.

```python
tab = chrome.find_tab(url_contains="mail.google.com")
tab.evaluate("document.title")           # reading is fine
tab.navigate("https://example.com")      # TabOwnershipError
tab.navigate("https://example.com", allow_foreign=True)   # explicit
```

---

## Install

```bash
pip install skynet-chrome-cdp
```

Or, because there is nothing to install:

```bash
curl -O https://raw.githubusercontent.com/Zek21/skynet-chrome-cdp/master/benchmarks/standalone_benchmark.py
python standalone_benchmark.py --port 9222
```

Start Chrome with a debugging port. **`--user-data-dir` is not optional:**

```bash
chrome --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-automation"
```

Since **Chrome 136**, `--remote-debugging-port` is ignored when it would open the
*default* user data directory. The flag only takes effect alongside a
`--user-data-dir` pointing somewhere non-standard, because a non-default
directory uses a different encryption key and so keeps the real profile's saved
passwords and cookies out of reach of the protocol.

Omit it and Chrome logs *"DevTools remote debugging requires a non-default data
directory"*, creates no listening socket and no `DevToolsActivePort` file, and
your script hangs on a blank page. If nothing is connecting, this is almost
always why.

That directory may hold a fully signed-in profile — it just cannot be the default
one. What Chrome closed is the command-line route into somebody's daily browser.

Kill any running Chrome first; a surviving instance means your relaunch quietly
reuses the default directory.

---

## Structural perception

Handing a language model raw HTML does not scale: a real application serialises
to 50k–500k characters, most of it framework noise. Screenshots trade that for a
harder problem — the model has to localise controls in pixels and cannot read
what is covered or scrolled away.

The third option is to ask the browser what it thinks the page *is*. Chrome
already computes roles, names and states for assistive technology.

```python
page = scene(tab)
page.summary()
# 240 reachable (18 on screen) of 240 candidates (120 button, 120 textbox);
# 55945 DOM chars -> 9712 scene chars, 5.76x

page.find("Submit 3").to_line()   # 'button "Submit 3" @229,412'
```

**On the numbers.** Projects in this space advertise reductions like "100k tokens
→ 1.4k". That ratio is a property of the *page*, not of the technique: an article
has a handful of controls and compresses enormously; a settings form has hundreds
and barely compresses at all. So `reduction_ratio` here is measured against the
scene actually emitted, for the page actually loaded, and it is `None` when the
scene came back empty — because dividing a large document by an empty scene
produces the biggest number the system can report at the exact moment it has
failed. That case is [Clause 7.5](SPEC.md#7-perception), and it is in the spec
because this implementation did it.

---

## Measured performance

Chrome 151, Windows 11, 6 cores, synthetic fixture (685 elements), n=200,
20 warm-up samples discarded:

| Operation | p50 | p90 | p99 |
|-----------|-----|-----|-----|
| `evaluate` round trip (sequential baseline) | **0.33 ms** | 0.46 ms | 2.10 ms |
| Scene extraction | 7.17 ms | — | — |
| Full DOM serialisation | 12.10 ms | — | — |
| Accessibility tree | 48.53 ms | — | — |
| Screenshot (PNG) | 78.27 ms | — | — |

**3,048 calls/second implied** by that median (1000/p50 — a derived rate, not measured throughput). Reproduce it yourself:

```bash
python -m skynet_chrome_cdp.benchmark --port 9222 --samples 200
```

The fixture is built by the benchmark inside a tab it opens, so no network is
involved and two machines are given an identical **fixture** — which is not the
same as identical browser-internal work; see
[CROSS_MACHINE.md](benchmarks/results/CROSS_MACHINE.md), where two hosts built
accessibility trees of 1,684 and 1,244 nodes from that same page. A run against
an arbitrary
open page sets `comparable_across_machines: false` in the report — a
machine-readable field, because a JSON file outlives the caveat that came with
it. Full procedure: [SPEC.md Annex B](SPEC.md#annex-b-informative--benchmark-procedure).

---

## The bridge and the extension

For work that CDP cannot reach — service-worker state, `chrome.*` APIs, a profile
launched without a debugging port — the connector pairs a local bridge process
with a browser extension. Two halves updated on different schedules will drift,
and drift here does not produce an error: commands get accepted, acknowledged,
and not performed.

So they negotiate before exchanging a single command, in **both** directions:

```python
from skynet_chrome_cdp import negotiate

negotiate(peer_version="1.0.11")
# accepted=False, close_code=4426,
# reason='peer 1.0.11 is older than the minimum 1.3.0; upgrade the extension'

negotiate(peer_version="2.0.0", peer_min_version="2.0.0", local_version="1.3.0")
# accepted=False, close_code=4426, reason='... upgrade the bridge'
```

The second case is the one implementations forget, and it is the one that fails
silently. There is no degraded mode: on mismatch the connection closes.

---

## Security

This library takes control of a browser holding live sessions. Read
[SECURITY.md](SECURITY.md) before pointing it at your daily profile.

- The bridge binds to loopback and **refuses** any other address — a DevTools
  port on a routable interface hands every host on the network every cookie in
  the profile.
- Requests carry a per-run secret, compared in constant time. Any page in the
  browser can reach `127.0.0.1`; without a secret, a visited site can drive the
  bridge that drives your browser.
- The secret is stored owner-only, verified through the mechanism the platform
  *actually* enforces. Writing `0600` on Windows produces a file governed by an
  inherited NTFS ACL; the mode bits are inert. This implementation shipped that
  bug and a test caught it — see Clause 9.4.

---

## Specification

[SPEC.md](SPEC.md) is a numbered, testable specification written in the structure
IEEE standards use. It is **not** an IEEE-published standard and does not claim to
be — it is written so it could be reviewed under that process. All 33 `shall`
clauses carry an Annex A row naming their evidence: an executable test where the
requirement reduces to one, a named artifact such as a manifest field where it
does not.

```bash
pytest tests/          # 85 tests
```

---

## What this is not

- **Not a scraper.** No proxy rotation, no CAPTCHA solving, no fingerprint
  spoofing.
- **Not headless-first.** It is for browsers with a human's session in them.
- **Not a Playwright replacement for testing.** For deterministic CI against a
  clean browser, use Playwright — that is what it is good at.

## License

MIT. See [LICENSE](LICENSE).
