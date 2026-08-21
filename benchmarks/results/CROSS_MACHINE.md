# Cross-machine results

Two different physical machines, the same synthetic fixture, 200 samples each,
20 warm-up samples discarded. The fixture is built by the benchmark inside a tab
it opens, so no network is involved and both machines measured identical work.

The remote runs were executed by an operator on the second machine, who fetched
`standalone_benchmark.py` over HTTPS and ran it with no install step — the
zero-dependency property is what made that possible.

## Hosts

| | A | B |
|---|---|---|
| Machine | `Zeke` | `DESKTOP-OJ9K9QR` |
| OS | Windows 11 (10.0.26200) | Windows 10 (10.0.19045) |
| CPUs | 6 | 4 |
| Python | 3.13.7 | 3.14.7 |
| Chrome | 151.0.7922.140 | 151.0.7922.140 |
| CDP | 1.3 | 1.3 |

## Protocol floor — `Runtime.evaluate("1+1")`, n=200

| Run | p50 | p90 | p99 | max | stdev | ops/s @p50 |
|-----|-----|-----|-----|-----|-------|-----------|
| A, run 1 | 0.3209 | 0.4459 | 2.2206 | 2.7451 | 0.3005 | 3116 |
| A, run 2 | 0.2996 | 0.3827 | 2.1797 | 2.4270 | 0.2789 | 3338 |
| B, run 1 | 0.3627 | 0.6641 | 2.1815 | **232.6731** | 16.4113 | 2757 |
| B, run 2 | 0.7598 | 1.1052 | 1.9931 | 2.4824 | 0.2887 | 1316 |

All times in milliseconds.

## Page operations (p50, ms)

| Operation | A | B |
|-----------|---|---|
| DOM serialisation | 11.14 – 11.87 | 21.35 – 24.15 |
| Accessibility tree | 46.90 – 47.17 | 85.85 – 87.52 |
| Screenshot (PNG) | 78.03 – 82.14 | 73.98 – 76.11 |

## What these numbers say

**The floor is sub-millisecond on both machines.** A p50 of 0.30–0.76 ms means
the protocol is not the bottleneck in any realistic automation workload; page
work is.

**Run B1 is the interesting one.** Its p50 (0.3627 ms) is better than run B2's
(0.7598 ms), but it contains a single 232 ms sample that drags the standard
deviation to 16.4 ms. Run B2 is nearly three times slower at the median and
vastly more predictable, maxing out at 2.48 ms.

Read the columns left to right and watch the stall come into focus.

At **p50 and p90**, B1 is simply the faster run. At **p99** it has already turned
— 2.1815 ms against B2's 1.9931 ms — but only slightly, and nothing in that
figure hints at a 232 ms event: one observation in two hundred sits at the 99.5th
percentile, above where p99 looks at all.

**Max and stdev** are where it becomes unmistakable: 232.6731 against 2.4824, and
16.4113 of spread against 0.2887.

The **mean** moves as well, in fairness: one 232 ms sample spread over 200
observations adds roughly 1.16 ms, which against a ~0.36 ms baseline is not
subtle. But it absorbs the outlier into an average rather than identifying it as
a single event — it reports a slower run, not a stalling one.

That is the argument for [Clause 8.5](../../SPEC.md) requiring minimum, median,
p90, p99, maximum *and* standard deviation rather than any subset of them. These
remain **summary statistics** — the full distribution would be all 200 samples —
but taken together they describe the shape, and a mean alone does not. A 232 ms
pause inside a loop of thousands of calls is what trips a production timeout, and
at n=200 only two of those columns say so plainly.

**Machine B is roughly 2× slower on DOM serialisation and accessibility-tree
extraction**, which is consistent with 4 cores against 6 — though nothing here
profiles the cause, so read it as correlation rather than explanation.

**Screenshot capture is the exception.** 73.98–76.11 ms on machine B against
78.03–82.14 ms on machine A: it did not scale with cores and was marginally
*faster* on the slower box. Something other than CPU parallelism dominates it.
This benchmark does not measure what, and no claim is made.

**The perception reduction was identical on both machines: 12.22×**, from 55,945
DOM characters to 4,579 scene characters. That is the fixture doing its job. A
figure that reproduces exactly across two different operating systems, core
counts, and Python versions is a property of the measurement, not of the machine
— which is what `comparable_across_machines: true` is asserting.

One difference worth recording: machine A's accessibility tree contained 1,684
nodes and machine B's 1,244, for the same DOM. The count of *actionable* nodes
was 240 on both. The full tree includes generic and ignored nodes whose
population varies by platform; the actionable subset does not.

## Defect found by the remote operator

`render_human()` computed `own_tab_closed` and never printed it, so the operator
could not state whether the benchmark had cleaned up after itself — and declined
to claim that it had, rather than assuming. Fixed: the field is now printed in
the human output, not only under `--json`.

A hygiene field that appears only in a machine-readable mode is a hygiene field
nobody checks.
