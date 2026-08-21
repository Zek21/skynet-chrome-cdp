"""Scene semantics, with the browser factored out.

`scene()` needs Chrome, but the arithmetic and the reporting rules do not, and
those are where a misleading number gets manufactured. The degenerate-scene guard
here corresponds to a real defect: an earlier build divided a 55,945-character DOM
by a 42-character empty scene and reported a 1332x reduction, on a page where the
perception layer had in fact seen nothing at all.
"""
from __future__ import annotations

import unittest

from skynet_chrome_cdp.perception import ACTIONABLE_ROLES, Element, Scene


def make_element(**kwargs) -> Element:
    base = dict(role="button", name="Save", x=10, y=20, w=80, h=30,
                in_viewport=True, occluded=False, actionable=True,
                disabled=False, selector="#save")
    base.update(kwargs)
    return Element(**base)


class ElementLineTest(unittest.TestCase):
    def test_line_carries_role_name_and_position(self):
        self.assertEqual(make_element().to_line(), 'button "Save" @10,20')

    def test_disabled_is_marked(self):
        self.assertIn("[disabled]", make_element(disabled=True).to_line())

    def test_covered_is_marked(self):
        self.assertIn("[covered]", make_element(occluded=True).to_line())

    def test_offscreen_is_marked_and_does_not_double_up_with_covered(self):
        line = make_element(in_viewport=False, occluded=False, actionable=False).to_line()
        self.assertIn("[offscreen]", line)
        self.assertNotIn("[covered]", line)

    def test_unnamed_element_still_produces_a_usable_line(self):
        line = make_element(name="").to_line()
        self.assertTrue(line.startswith("button @"))


class SceneSelectionTest(unittest.TestCase):
    def setUp(self):
        self.scene = Scene(
            url="https://example.com/form",
            title="Form",
            dom_chars=50_000,
            elements=[
                make_element(name="Visible", actionable=True, in_viewport=True),
                make_element(name="Below", actionable=False, in_viewport=False),
                make_element(name="Covered", actionable=False, occluded=True),
                make_element(name="Off", disabled=True),
            ],
        )

    def test_actionable_is_only_what_can_be_clicked_now(self):
        self.assertEqual([e.name for e in self.scene.actionable], ["Visible"])

    def test_reachable_includes_offscreen_because_scrolling_exists(self):
        """A scene limited to the viewport describes a long page as empty."""
        self.assertEqual([e.name for e in self.scene.reachable],
                         ["Visible", "Below", "Covered"])

    def test_disabled_is_excluded_from_both(self):
        self.assertNotIn("Off", [e.name for e in self.scene.reachable])
        self.assertNotIn("Off", [e.name for e in self.scene.actionable])

    def test_to_text_defaults_to_the_reachable_working_set(self):
        text = self.scene.to_text()
        self.assertIn("Visible", text)
        self.assertIn("Below", text)
        self.assertIn("Form — https://example.com/form", text)

    def test_to_text_can_be_narrowed_to_onscreen_only(self):
        text = self.scene.to_text(actionable_only=True)
        self.assertIn("Visible", text)
        self.assertNotIn("Below", text)


class ReductionArithmeticTest(unittest.TestCase):
    def test_ratio_is_dom_over_measured_scene(self):
        scene = Scene(dom_chars=1000, elements=[make_element(name="A")])
        self.assertEqual(scene.reduction_ratio,
                         round(1000 / scene.scene_chars, 2))

    def test_empty_scene_on_a_real_page_is_degenerate_not_infinite_gain(self):
        scene = Scene(dom_chars=55_945, elements=[])
        self.assertTrue(scene.degenerate)
        self.assertIsNone(scene.reduction_ratio,
                          "a scene that perceived nothing must not report a ratio")

    def test_all_disabled_also_counts_as_degenerate(self):
        scene = Scene(dom_chars=9000, elements=[make_element(disabled=True)])
        self.assertTrue(scene.degenerate)
        self.assertIsNone(scene.reduction_ratio)

    def test_truly_empty_page_is_not_flagged_degenerate(self):
        """No DOM and no elements is consistent, not a perception failure."""
        self.assertFalse(Scene(dom_chars=0, elements=[]).degenerate)

    def test_summary_says_n_a_instead_of_printing_a_bogus_number(self):
        self.assertIn("n/a", Scene(dom_chars=55_945, elements=[]).summary())


class FindTest(unittest.TestCase):
    def setUp(self):
        self.scene = Scene(dom_chars=100, elements=[
            make_element(name="Save and exit"),
            make_element(name="Save"),
            make_element(name="Cancel", role="link"),
        ])

    def test_exact_match_wins_over_a_longer_substring_match(self):
        """'Save' must not select 'Save and exit' when 'Save' exists."""
        self.assertEqual(self.scene.find("Save").name, "Save")

    def test_substring_match_is_the_fallback(self):
        self.assertEqual(self.scene.find("and exit").name, "Save and exit")

    def test_match_is_case_insensitive_and_trims(self):
        self.assertEqual(self.scene.find("  cANCEL "), self.scene.find("Cancel"))

    def test_role_filter_applies(self):
        self.assertIsNone(self.scene.find("Cancel", role="button"))
        self.assertIsNotNone(self.scene.find("Cancel", role="link"))

    def test_missing_returns_none(self):
        self.assertIsNone(self.scene.find("Nonexistent"))


class RoleSetTest(unittest.TestCase):
    def test_covers_the_controls_that_matter(self):
        for role in ("button", "link", "textbox", "checkbox", "radio",
                     "combobox", "searchbox", "tab", "switch"):
            self.assertIn(role, ACTIONABLE_ROLES)

    def test_is_a_closed_set(self):
        """An open rule ('anything with onclick') readmits the noise the module
        exists to remove."""
        for role in ("div", "span", "paragraph", "generic", "text"):
            self.assertNotIn(role, ACTIONABLE_ROLES)


if __name__ == "__main__":
    unittest.main()
