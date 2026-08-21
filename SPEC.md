# Specification for a Session-Attached Browser Automation Connector

**Document:** SKY-CDP-1  
**Version:** 1.3.0  
**Status:** Draft for review  
**Date:** 2026-08-21

This document is written in the structure and language conventions used by IEEE
standards (IEEE Std 830 / 1016 / 29148 lineage). It is **not** an IEEE-published
standard and makes no claim to that status. It is a specification prepared so
that it *could* be reviewed under that process: every normative statement is
numbered, checkable, and carries a row in Annex A naming the evidence that
satisfies it — an executable test where the requirement reduces to one, and a
named artifact where it does not.

---

## 1. Overview

### 1.1 Scope

This specification defines the architecture, wire protocol, security boundary,
and measurement methodology for a **session-attached browser automation
connector**: software that controls a web browser instance which is *already
running and already authenticated as a human user*, rather than launching a
fresh, empty browser.

It covers:

- a) the transport and command layer between an automation process and a browser
     exposing the Chrome DevTools Protocol (CDP);
- b) the perception layer that reduces a rendered page to a set of operable
     elements;
- c) version negotiation and authentication between a local bridge process and a
     browser extension;
- d) the procedure by which performance claims about such a connector are
     measured and reported.

It does not cover browser installation, proxy configuration, credential
management, or the lawfulness of any particular automation task.

### 1.2 Purpose

Two classes of defect motivate this specification.

**Silent divergence.** A connector has two halves that are updated on different
schedules — a local process and a browser extension. When they drift, the
observed failure is not an error. Commands are accepted, acknowledged, and not
performed. The caller records success. Clause 6 makes that state unreachable.

**Unfalsifiable performance claims.** Published browser-automation benchmarks
routinely omit the fixture, the sample count, the percentile, and the machine,
which makes them unreproducible and therefore unreviewable. Clause 8 defines a
procedure that a third party can re-run and disagree with.

### 1.3 Word usage

- **shall** — a mandatory requirement. An implementation that violates a *shall*
  is non-conforming.
- **should** — a recommendation. Deviation is permitted where justified.
- **may** — an optional feature.

### 1.4 Conformance

An implementation is **conforming** if it satisfies every *shall* in Clauses 4
through 9 and satisfies the corresponding Annex A evidence row — passing the
named test where the row cites one, and exhibiting the named artifact where it
does not. Partial conformance
shall not be claimed; a connector that satisfies Clause 5 but not Clause 7 is
non-conforming and shall be described as such.

Clauses 4–9 contain **33** *shall*-bearing clauses and Annex A carries a row for
each. Some rows cite an executable test; others cite the artifact that satisfies
the requirement, such as a manifest field or a report key, because not every
requirement of this kind reduces to a unit test. Both forms are auditable, and
the distinction is stated rather than blurred.

An earlier revision claimed every statement was mapped while **15** had no Annex A
entry at all. Traceability is worth claiming only after somebody has counted.

---

## 2. Normative references

- RFC 6455, *The WebSocket Protocol*
- RFC 2119 / RFC 8174, *Key words for use in RFCs*
- Chrome DevTools Protocol, version 1.3
- W3C, *Accessible Rich Internet Applications (WAI-ARIA) 1.2*
- W3C, *HTML Accessibility API Mappings*

---

## 3. Definitions

**3.1 session-attached** — operating on a browser process not started by the
automation software, retaining that browser's cookies, storage, extensions, and
authenticated sessions.

**3.2 owned tab** — a browser tab created by the connector during the current
process lifetime.

**3.3 foreign tab** — any tab that is not an owned tab, including every tab the
human user opened.

**3.4 scene** — a textual reduction of a rendered document to the elements a user
could operate, one line per element.

**3.5 reachable element** — an element that is rendered, enabled, and can be
operated after scrolling.

**3.6 on-screen element** — a reachable element whose centre point is currently
within the viewport and returns itself (or a descendant) from a hit test.

**3.7 degenerate scene** — a scene containing no reachable elements extracted
from a document whose serialised length is greater than zero.

**3.8 sequential baseline** — the measured
round-trip latency of a single CDP request that performs no page work, issued
with no other request in flight.

*It is a measured baseline for reading other operations against, NOT a
theoretical minimum: batching, pipelining and concurrent sessions are outside
what it measures, and nothing in this specification establishes that the figure
cannot be beaten.*

---

## 4. Architecture

**4.1** The connector **shall** be decomposed into four layers, each usable
without the layers above it:

| Layer | Responsibility | Depends on |
|-------|----------------|-----------|
| L1 Transport | RFC 6455 framing | standard library only |
| L2 Protocol | CDP request/response, target lifecycle, ownership | L1 |
| L3 Perception | document → scene reduction | L2 |
| L4 Bridge | extension pairing, negotiation, authentication | L1, L2 |

**4.2** L1 and L2 **shall not** require any package outside the language's
standard library. *Rationale: the machines on which session-attached automation
is most needed — locked-down corporate images, containers built from a lockfile,
hosts reachable only through a command channel — are frequently machines on which
new packages cannot be installed. A connector that cannot be transmitted as a
file and executed does not run where it is needed.*

**4.3** A higher layer **shall not** be required in order to use a lower one.

---

## 5. Target lifecycle and non-interference

**5.1** The connector **shall** classify every target as an owned tab or a
foreign tab (3.2, 3.3).

**5.2** The connector **shall** refuse any operation that navigates, mutates the
DOM of, injects input into, or closes a foreign tab, unless the caller supplies
an explicit per-call override.

*Rationale: the browser under control holds the user's authenticated sessions. An
automation error that navigates "the current tab" may act on the user's mail,
bank, or an unsaved document. Refusal by default converts a data-loss event into
an exception.*

**5.3** Read-only operations on a foreign tab **shall** be permitted without an
override, since target discovery requires them.

**5.4** Closure of an owned tab **shall** be confirmed by re-enumerating targets
and observing the target's absence. The connector **shall not** treat the close
endpoint's response body as evidence of closure. *Rationale: the endpoint replies
`Target is closing`, a statement of intent. It is also not JSON, and an
implementation that parses it as JSON will report a failure for a tab that did
in fact close.*

**5.5** On normal termination the connector **shall** close every owned tab.

**5.6** The connector **shall not** alter persistent profile state — cookies,
local storage, IndexedDB — except as directly requested by a caller.

---

## 6. Version negotiation

**6.1** Before any command is exchanged, each half **shall** transmit its own
protocol version and the minimum peer version it accepts.

**6.2** Negotiation **shall** be bidirectional. Each side **shall** evaluate both
conditions:

- a) peer version ≥ local minimum;
- b) local version ≥ peer's stated minimum.

*Rationale: implementations commonly check only (a). Condition (b) is the one
that produces silent divergence — a newer extension issuing commands an older
bridge accepts and does not implement.*

**6.3** On failure the connection **shall** be closed with a code identifying the
cause, drawn from the private range of RFC 6455 §7.4.2:

| Code | Condition |
|------|-----------|
| 4400 | hello frame unparseable |
| 4401 | authentication missing or invalid |
| 4403 | profile not paired |
| 4409 | request origin not the paired extension |
| 4426 | version outside the accepted range |

**6.4** A connector **shall not** enter a degraded or partial mode on negotiation
failure. It **shall** close.

**6.5** Version comparison **shall** be numeric per component. *Rationale:
lexical comparison orders `1.10.0` before `1.9.0`, silently rejecting a newer
peer.*

---

## 7. Perception

**7.1** The perception layer **shall** classify each candidate element as
reachable (3.5), on-screen (3.6), or neither, and **shall** report the three
counts separately.

**7.2** On-screen classification **shall** be determined by a hit test at the
element's centre point, accepting the element or a descendant. *Rationale: this
is the resolution a real click performs; geometry alone does not account for
overlays, consent banners, or stacking context.*

**7.3** A scene **shall** include reachable elements that are outside the
viewport, marked as such. *Rationale: on a long document nearly every control is
outside the viewport. A scene restricted to on-screen elements describes such a
page as empty, and the agent consuming it concludes there is nothing to do.*

**7.4** Where a reduction ratio is reported, it **shall** be computed from the
measured length of the emitted scene, not from an assumed per-element cost.

**7.5** A connector **shall not** report a reduction ratio for a degenerate scene
(3.7). It **shall** report the degenerate condition instead.

*Rationale: this requirement exists because of an observed defect. A build of the
reference implementation divided a 55,945-character document by a 42-character
empty scene and reported a 1332× reduction — the largest figure it ever produced,
on a page where perception had failed completely. Any metric of the form
`before/after` will report a failure of the "after" stage as a spectacular
success unless this case is explicitly excluded.*

**7.6** Reported reduction ratios **shall** be accompanied by an identification
of the document measured, and **shall not** be presented as a property of the
connector. *Rationale: the ratio is dominated by the document. A form-dense page
reduces by roughly 5×; an article by two orders of magnitude more.*

---

## 8. Measurement methodology

**8.1** Any published latency or throughput figure **shall** be accompanied by:

- a) the fixture, defined precisely enough to be rebuilt;
- b) the sample count;
- c) the percentile, and the method used to compute it;
- d) the host: CPU, operating system, browser build, runtime version.

**8.2** A conforming benchmark **shall** provide a synthetic fixture that it
constructs itself, in a tab it owns, without network access. *Rationale: any
fixture fetched over a network measures the network. Any fixture that is "the
page that happened to be open" is not comparable between two hosts.*

**8.3** A benchmark run against an arbitrary existing page **shall** mark its
report as not comparable across hosts, in a machine-readable field. *Rationale: a
JSON report outlives the caveat in the prose that accompanied it.*

**8.4** The benchmark **shall** report the sequential baseline (3.8) separately from
any higher-level operation, so that library overhead can be distinguished from
protocol and browser cost.

**8.5** The benchmark **shall** report a distribution — minimum, median, 90th and
99th percentile, maximum, standard deviation — and **shall not** report a mean
alone. *Rationale: automation reliability is governed by the tail.*

**8.6** The benchmark **shall** discard warm-up samples and **shall** state how
many were discarded.

**8.7** The benchmark **shall** report whether it left any tab open.

---

## 9. Security

**9.1** The bridge **shall** bind only to a loopback address, and **shall**
refuse any other bind address rather than warn. *Rationale: a DevTools port or
bridge on a routable address grants every host on the network read access to
every cookie in an authenticated browser profile.*

**9.2** Every request to the bridge **shall** carry a shared secret generated per
run, compared in constant time.

*Rationale: any page loaded in the browser can issue requests to `127.0.0.1`. In
the absence of a secret, a visited website can drive the bridge that drives the
user's authenticated browser.*

**9.3** The bridge **shall** verify that the request origin matches the paired
extension.

**9.4** The shared secret **shall** be stored such that no principal other than
the current user and a documented set of tolerated privileged principals may read
it, and the implementation **shall** verify this using the mechanism the platform
actually enforces. The tolerated set **shall** be enumerated, and each entry
**shall** be justified by the fact that excluding it would not deny that
principal access.

*Rationale for "tolerated" rather than "unavoidable": on Windows a DACL can in
fact be written without an Administrators ACE. The reason to permit one is
narrower — a local administrator can take ownership of any file and rewrite its
DACL, so removing the entry changes the audit trail rather than who can read the
secret, while causing the check to fail on a correctly protected file. Calling
such principals unavoidable overstates the platform constraint and invites an
implementation to tolerate more than it should.*

*Rationale: writing a secret with POSIX mode `0600` on Windows produces a file
whose read access is decided by an inherited NTFS ACL. The mode bits are not
wholly discarded — the write bit maps onto the read-only attribute — but nothing
in them yields owner-only read semantics.
An implementation that checks only the mode bits reports a protection it does not
have.*

**9.5** A connector **shall not** be distributed containing extension signing
keys, machine-generated runtime extension copies, profile identifiers, or
authentication tokens.

**9.6** Host permissions requested by the extension **shall** be the minimum
required. *Rationale: a broadly-permissioned extension combined with an
unauthenticated local listener is a local privilege escalation path — any local
process or visited page can execute authenticated actions in the user's session.*

---

## Annex A (normative) — Conformance test mapping

Each requirement carries a row naming its evidence. Where that evidence is a
test, `pytest tests/` runs it; where the requirement is satisfied by an artifact —
a manifest field, a report key, an ignore rule — the row names the artifact
instead of pretending a unit test covers it.

| Clause | Requirement | Evidence (executable test, or named artifact) |
|--------|-------------|------|
| 4.2 | stdlib only | `test_transport.py` (imports), `pyproject.toml` has no runtime deps |
| 5.2 | foreign tab mutation refused | `test_cdp.py::test_mutating_a_foreign_tab_raises` |
| 5.2 | explicit override honoured | `test_cdp.py::test_allow_foreign_opts_in_explicitly` |
| 5.3 | foreign reads permitted | `test_cdp.py::test_reading_a_foreign_tab_is_allowed` |
| 5.4 | closure confirmed by re-enumeration | `test_cdp.py::test_a_tab_still_listed_reports_failure` |
| 5.4 | plain-text reply is not a failure | `test_cdp.py::test_plain_text_close_reply_is_not_a_failure` |
| 5.5 | owned tabs closed on exit | `test_cdp.py::test_context_manager_closes_owned_tabs` |
| 6.2a | old peer refused | `test_handshake.py::test_old_peer_is_refused_with_upgrade_required` |
| 6.2b | old local side refused | `test_handshake.py::test_old_LOCAL_side_is_also_refused` |
| 6.3 | malformed hello refused | `test_handshake.py::test_malformed_hello_is_refused_not_defaulted` |
| 6.4 | no degraded mode | `test_handshake.py::test_unauthenticated_1_0_11_can_never_be_accepted` |
| 6.5 | numeric version ordering | `test_handshake.py::test_double_digit_minor_compares_numerically` |
| 7.1 | three counts reported | `test_perception.py::SceneSelectionTest` |
| 7.3 | offscreen included and marked | `test_perception.py::test_reachable_includes_offscreen_because_scrolling_exists` |
| 7.4 | ratio from measured length | `test_perception.py::test_ratio_is_dom_over_measured_scene` |
| 7.5 | degenerate scene reports no ratio | `test_perception.py::test_empty_scene_on_a_real_page_is_degenerate_not_infinite_gain` |
| 8.1–8.6 | distribution and fixture reported | `test_cdp.py::BenchmarkStatisticsTest` |
| 8.3 | non-comparable runs marked | `benchmark.py` field `comparable_across_machines` |
| 9.1 | non-loopback refused | `test_handshake.py::test_wildcard_bind_is_refused` |
| 9.2 | constant-time comparison | `test_handshake.py::test_verify_accepts_only_the_exact_token` |
| 9.3 | origin verified | `test_handshake.py::test_origin_must_match_the_paired_extension` |
| 4.1 | four layers, each usable alone | package exposes `transport`, `cdp`, `perception`, `handshake` independently; `test_transport.py` imports L1 only |
| 4.3 | no upward dependency | `test_transport.py` (L1 tested without L2–L4) |
| 5.1 | every target classified owned/foreign | `test_cdp.py::test_a_listed_tab_is_foreign`, `test_new_tab_is_owned_and_mutable` |
| 5.6 | profile state not altered | no cookie/storage write exists in `cdp.py`; `Tab.cookies()` is read-only |
| 6.1 | versions exchanged before commands | `test_handshake.py::test_matching_versions_are_accepted` |
| 7.2 | on-screen decided by hit test | `perception.py` `_COLLECT_JS` `elementFromPoint`; `test_perception.py::test_actionable_is_only_what_can_be_clicked_now` |
| 7.6 | ratio names the document measured | `benchmark.py` emits `fixture`, `fixture_detail` beside `reduction_ratio` |
| 8.1 | fixture, samples, percentile, host published | `benchmark.py` report fields `fixture`, `evaluate_rtt.n`, `host` |
| 8.2 | self-built synthetic fixture, no network | `test_cdp.py::test_fixture_declares_its_own_dimensions`, `test_fixture_interleaves_controls_with_text` |
| 8.4 | sequential baseline reported separately | `benchmark.py` `measurements.evaluate_rtt` |
| 8.5 | distribution, not a mean alone | `test_cdp.py::test_summary_matches_hand_computed_values` |
| 8.6 | warm-up discarded and stated | `benchmark.py` `WARMUP_SAMPLES` |
| 8.7 | reports whether a tab was left open | `test_cdp.py::test_a_tab_still_listed_reports_failure` |
| 9.5 | no keys/profile ids/tokens distributed | `.gitignore` excludes `*.pem`, `*.crx`, `token`; extension ships no `key` |
| 9.6 | minimum host permissions | `extension/manifest.json` `host_permissions` is `http://127.0.0.1/*` only |
| 9.4 | platform-enforced secrecy | `test_handshake.py::test_token_file_is_private_on_this_platform` |
| 9.4 | check can still fail | `test_handshake.py::test_privacy_check_catches_a_grant_to_everyone` |

## Annex B (informative) — Benchmark procedure

1. Confirm a browser is listening on the DevTools port.
2. Open a tab owned by the benchmark, at `about:blank`.
3. Construct the synthetic fixture in that tab: 120 interactive controls and 200
   text blocks, interleaved so that controls occur both above and below the fold.
4. Discard 20 warm-up samples.
5. Record 200 samples of `Runtime.evaluate("1+1")` — the sequential baseline.
6. Record 20 samples each of document serialisation, scene extraction, and
   accessibility tree retrieval; 10 of screenshot capture.
7. Close the owned tab and confirm its absence.
8. Emit the distribution for each measurement, the fixture description, the host
   description, and the comparability flag.

## Annex C (informative) — Reference measurements

Recorded on the reference implementation. These are observations from specific
machines, not conformance targets.

| Host | Browser | Baseline p50 | p99 | Implied rate (1000/p50) | Scene | Reduction |
|------|---------|-----------|-----|-----------|-------|-----------|
| Windows 11, 6 cores | Chrome 151.0.7922.140 | 0.33 ms | 2.10 ms | 3,048 calls/s | 7.2 ms | 5.76× |

The reduction figure is for the synthetic fixture, which is deliberately
form-dense: scene length scales with the number of controls while the document
length does not, so a control-dense page is the condition under which this
technique reduces least (7.6). No claim is made about where this fixture sits
relative to a true worst case; only one fixture was measured.
