"""Tab ownership, close confirmation, and benchmark arithmetic.

The ownership rules are the safety-relevant part of this library: it attaches to
a browser containing the user's real sessions, so "which tabs may this process
touch" is a correctness property, not a nicety.
"""
from __future__ import annotations

import unittest
from unittest import mock

from skynet_chrome_cdp import benchmark
from skynet_chrome_cdp.cdp import CDPError, Chrome, Tab, TabOwnershipError


def target(target_id="T1", url="https://example.com", title="Example", kind="page"):
    return {
        "id": target_id,
        "url": url,
        "title": title,
        "type": kind,
        "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{target_id}",
    }


class OwnershipTest(unittest.TestCase):
    def setUp(self):
        self.chrome = Chrome(port=9222)

    def test_a_listed_tab_is_foreign(self):
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value=[target()]):
            tab = self.chrome.tabs()[0]
        self.assertFalse(tab.owned)

    def test_mutating_a_foreign_tab_raises(self):
        tab = Tab(self.chrome, target(), owned=False)
        for call in (
            lambda: tab.navigate("https://evil.example.com"),
            lambda: tab.click("#pay"),
            lambda: tab.type_text("#msg", "hi"),
        ):
            with self.assertRaises(TabOwnershipError):
                call()

    def test_reading_a_foreign_tab_is_allowed(self):
        """Reading is how you find the tab you want; only mutation is guarded."""
        tab = Tab(self.chrome, target(), owned=False)
        with mock.patch.object(Tab, "call", return_value={
                "result": {"value": "Example"}}):
            self.assertEqual(tab.evaluate("document.title"), "Example")

    def test_allow_foreign_opts_in_explicitly(self):
        tab = Tab(self.chrome, target(), owned=False)
        with mock.patch.object(Tab, "evaluate", return_value=True) as ev:
            self.assertTrue(tab.click("#ok", allow_foreign=True))
        ev.assert_called_once()

    def test_new_tab_is_owned_and_mutable(self):
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value=target("NEW")):
            tab = self.chrome.new_tab("about:blank")
        self.assertTrue(tab.owned)
        self.assertIn("NEW", self.chrome._owned_tab_ids)

    def test_closing_a_foreign_tab_raises(self):
        with self.assertRaises(TabOwnershipError):
            self.chrome.close_tab("SOMEONE-ELSES-TAB")

    def test_new_tab_falls_back_to_GET_when_PUT_is_rejected(self):
        """Chrome < 111 rejects PUT on /json/new."""
        calls = []

        def fake(port, path, method="GET", timeout=8.0):
            calls.append(method)
            if method == "PUT":
                raise CDPError("HTTP 405")
            return target("OLD")

        with mock.patch("skynet_chrome_cdp.cdp._http_json", side_effect=fake):
            tab = self.chrome.new_tab()
        self.assertEqual(calls, ["PUT", "GET"])
        self.assertTrue(tab.owned)

    def test_new_tab_raises_when_chrome_returns_nothing_usable(self):
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value={}):
            with self.assertRaises(CDPError):
                self.chrome.new_tab()


class CloseConfirmationTest(unittest.TestCase):
    """The close endpoint replies 'Target is closing' -- intent, not outcome."""

    def setUp(self):
        self.chrome = Chrome(port=9222)
        self.chrome._owned_tab_ids.add("T1")

    def test_close_is_confirmed_by_relisting_targets(self):
        responses = ["Target is closing", []]  # close reply, then empty list
        with mock.patch("skynet_chrome_cdp.cdp._http_json",
                        side_effect=lambda *a, **k: responses.pop(0)):
            self.assertTrue(self.chrome.close_tab("T1"))

    def test_a_tab_still_listed_reports_failure(self):
        """A tab that refuses to close must not be reported as closed."""
        responses = ["Target is closing", [target("T1")]]
        with mock.patch("skynet_chrome_cdp.cdp._http_json",
                        side_effect=lambda *a, **k: responses.pop(0)):
            self.assertFalse(self.chrome.close_tab("T1"))

    def test_plain_text_close_reply_is_not_a_failure(self):
        """Parsing that reply as JSON raises, and an earlier build turned the
        raise into 'could not close the tab' for a tab that HAD closed."""
        responses = ["Target is closing", []]
        with mock.patch("skynet_chrome_cdp.cdp._http_json",
                        side_effect=lambda *a, **k: responses.pop(0)):
            self.assertTrue(self.chrome.close_tab("T1"))

    def test_context_manager_closes_owned_tabs(self):
        chrome = Chrome(port=9222)
        chrome._owned_tab_ids.update({"A", "B"})
        with mock.patch("skynet_chrome_cdp.cdp._http_json",
                        side_effect=lambda *a, **k: []):
            with chrome:
                pass
        self.assertEqual(chrome._owned_tab_ids, set())


class DiscoveryTest(unittest.TestCase):
    def test_devtools_windows_are_excluded_from_tabs(self):
        listing = [target("A"), target("B", url="devtools://devtools/bundled/x.html")]
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value=listing):
            self.assertEqual([t.id for t in Chrome().tabs()], ["A"])

    def test_targets_without_a_socket_are_excluded(self):
        broken = target("C")
        del broken["webSocketDebuggerUrl"]
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value=[target("A"), broken]):
            self.assertEqual([t.id for t in Chrome().tabs()], ["A"])

    def test_find_tab_matches_url_and_title(self):
        listing = [target("A", url="https://linkedin.com/feed", title="Feed"),
                   target("B", url="https://example.com", title="Example")]
        with mock.patch("skynet_chrome_cdp.cdp._http_json", return_value=listing):
            chrome = Chrome()
            self.assertEqual(chrome.find_tab(url_contains="linkedin").id, "A")
            self.assertEqual(chrome.find_tab(title_contains="example").id, "B")
            self.assertIsNone(chrome.find_tab(url_contains="nowhere"))

    def test_is_up_is_false_when_nothing_is_listening(self):
        with mock.patch("skynet_chrome_cdp.cdp._http_json",
                        side_effect=CDPError("refused")):
            self.assertFalse(Chrome(port=1).is_up())

    def test_attach_without_a_socket_url_explains_the_target_type(self):
        info = target("SW", kind="service_worker")
        del info["webSocketDebuggerUrl"]
        with self.assertRaises(CDPError) as ctx:
            Tab(Chrome(), info).attach()
        self.assertIn("service_worker", str(ctx.exception))


class BenchmarkStatisticsTest(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(benchmark.percentile(values, 50), 5)
        self.assertEqual(benchmark.percentile(values, 90), 9)
        self.assertEqual(benchmark.percentile(values, 100), 10)

    def test_percentile_never_runs_off_the_end(self):
        self.assertEqual(benchmark.percentile([7], 99), 7)
        self.assertEqual(benchmark.percentile([1, 2], 99), 2)

    def test_percentile_of_empty_is_none(self):
        self.assertIsNone(benchmark.percentile([], 50))

    def test_summary_matches_hand_computed_values(self):
        stats = benchmark.summarize([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
        self.assertEqual(stats["n"], 8)
        self.assertEqual(stats["mean_ms"], 5.0)
        self.assertEqual(stats["stdev_ms"], 2.0)
        self.assertEqual(stats["min_ms"], 2.0)
        self.assertEqual(stats["max_ms"], 9.0)

    def test_empty_summary_reports_zero_rather_than_inventing_numbers(self):
        self.assertEqual(benchmark.summarize([]), {"n": 0})

    def test_fixture_declares_its_own_dimensions(self):
        self.assertIn(str(benchmark.FIXTURE_INTERACTIVE), benchmark.FIXTURE_JS)
        self.assertIn(str(benchmark.FIXTURE_TEXT_BLOCKS), benchmark.FIXTURE_JS)

    def test_fixture_interleaves_controls_with_text(self):
        """Stacking all text first pushed every control below the fold, and the
        run reported a page with nothing on it."""
        self.assertIn("INTERACTIVE; i++", benchmark.FIXTURE_JS)
        self.assertIn("textWritten", benchmark.FIXTURE_JS)


if __name__ == "__main__":
    unittest.main()
