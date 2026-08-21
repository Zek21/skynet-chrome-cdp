"""Structural perception: a page as a short list of things an agent can act on.

THE PROBLEM
-----------
Handing a language model raw HTML does not work at realistic page sizes. A modern
application serialises to tens or hundreds of thousands of characters, most of it
framework attributes, inline styles, and tracking markup. Screenshots avoid that
but replace it with a harder problem: the model must localise a control in pixels,
and it cannot read what is clipped, scrolled out, or behind an overlay.

The third option is to ask the browser what it thinks the page IS. Chrome already
computes an accessibility tree -- roles, names, states -- because assistive
technology needs one. That tree is a semantic description of the page produced by
the renderer itself, and it is far smaller than the DOM.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
Compression ratios quoted for this technique ("100k tokens down to 1.4k") are
properties of the PAGE, not of the technique. A page with 400 form fields has 400
actionable elements and compresses badly; an article has a handful and compresses
enormously. `scene()` therefore reports the measured before/after sizes for the
page it actually looked at, and `benchmark.py` states its fixture. A single
headline ratio with no fixture attached is marketing, not measurement.

VISIBILITY
----------
An element in the accessibility tree is not necessarily an element a user can
click. It may be scrolled out of view, clipped to zero size, or covered by a
consent banner. `visible_only=True` (the default) resolves this by hit-testing:
the element's own centre point is passed to `document.elementFromPoint`, and the
element is reported actionable only if the browser hands back that element or one
of its descendants. That is the same test a real click performs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .cdp import Tab

__all__ = ["Element", "Scene", "ACTIONABLE_ROLES", "scene"]

# ARIA roles that correspond to something a user can operate. Deliberately a
# closed set: an open-ended "anything with an onclick" rule readmits the noise
# this module exists to remove.
ACTIONABLE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
    "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "switch", "slider", "spinbutton", "textarea",
})

# The minimum fraction of an element's own box that must be unobstructed for it
# to be reported as actionable. A control 60% covered by a sticky header is one a
# click can still land on; one 90% covered usually is not.
DEFAULT_VISIBILITY_THRESHOLD = 0.60

_COLLECT_JS = """
(() => {
  const ROLES = %s;
  const THRESHOLD = %f;
  const out = [];
  const all = document.querySelectorAll(
    'a[href], button, input, select, textarea, summary, [role], [tabindex], [onclick]'
  );
  const roleOf = (el) => {
    const explicit = (el.getAttribute('role') || '').toLowerCase();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : '';
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'number') return 'spinbutton';
      if (t === 'search') return 'searchbox';
      if (t === 'submit' || t === 'button' || t === 'reset') return 'button';
      if (t === 'hidden') return '';
      return 'textbox';
    }
    return '';
  };
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const ref = document.getElementById(labelledby);
      if (ref && ref.textContent) return ref.textContent.trim();
    }
    if (el.id) {
      const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (lab && lab.textContent) return lab.textContent.trim();
    }
    const closest = el.closest('label');
    if (closest && closest.textContent) return closest.textContent.trim();
    if (el.tagName === 'INPUT' && el.placeholder) return el.placeholder.trim();
    if (el.value && el.tagName === 'INPUT') return String(el.value).trim();
    return (el.innerText || el.textContent || '').trim();
  };
  for (const el of all) {
    const role = roleOf(el);
    if (!ROLES.includes(role)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
    const inViewport = rect.bottom > 0 && rect.right > 0 &&
      rect.top < (window.innerHeight || 0) && rect.left < (window.innerWidth || 0);
    // Hit-test the centre: the same resolution a real click performs.
    let occluded = false, hitOk = false;
    if (inViewport) {
      const cx = Math.min(Math.max(rect.left + rect.width / 2, 1), (window.innerWidth || 1) - 1);
      const cy = Math.min(Math.max(rect.top + rect.height / 2, 1), (window.innerHeight || 1) - 1);
      const hit = document.elementFromPoint(cx, cy);
      hitOk = !!hit && (hit === el || el.contains(hit) || hit.contains(el));
      occluded = !hitOk;
    }
    out.push({
      role: role,
      name: nameOf(el).replace(/\\s+/g, ' ').slice(0, 100),
      x: Math.round(rect.left + rect.width / 2),
      y: Math.round(rect.top + rect.height / 2),
      w: Math.round(rect.width),
      h: Math.round(rect.height),
      in_viewport: inViewport,
      occluded: occluded,
      actionable: inViewport && hitOk,
      disabled: !!el.disabled,
      selector: el.id ? ('#' + CSS.escape(el.id)) : ''
    });
  }
  return {
    elements: out,
    title: document.title,
    url: location.href,
    dom_chars: document.documentElement.outerHTML.length,
    element_count: document.getElementsByTagName('*').length,
    scroll_y: Math.round(window.scrollY),
    viewport: {w: window.innerWidth, h: window.innerHeight}
  };
})()
""" % (json.dumps(sorted(ACTIONABLE_ROLES)), DEFAULT_VISIBILITY_THRESHOLD)


@dataclass(frozen=True)
class Element:
    """One thing on the page an agent could operate."""

    role: str
    name: str
    x: int
    y: int
    w: int = 0
    h: int = 0
    in_viewport: bool = True
    occluded: bool = False
    actionable: bool = True
    disabled: bool = False
    selector: str = ""

    def to_line(self) -> str:
        """One compact line. This is the unit the token budget is spent on."""
        parts = [self.role]
        if self.name:
            parts.append(f'"{self.name}"')
        parts.append(f"@{self.x},{self.y}")
        if self.disabled:
            parts.append("[disabled]")
        if self.occluded:
            parts.append("[covered]")
        elif not self.in_viewport:
            parts.append("[offscreen]")
        return " ".join(parts)


@dataclass
class Scene:
    """A page reduced to what can be acted on, with the arithmetic shown."""

    url: str = ""
    title: str = ""
    elements: list[Element] = field(default_factory=list)
    dom_chars: int = 0
    element_count: int = 0
    viewport: dict = field(default_factory=dict)
    scroll_y: int = 0

    @property
    def actionable(self) -> list[Element]:
        """Operable RIGHT NOW: on screen, not covered, not disabled."""
        return [e for e in self.elements if e.actionable and not e.disabled]

    @property
    def reachable(self) -> list[Element]:
        """Everything the agent can get to, including below the fold.

        This is the working set, and it is what `to_text()` reports. The
        distinction matters more than it looks: on a long page almost every
        control is outside the viewport, so a scene limited to `actionable`
        describes a page as having nothing on it. Off-screen controls are
        included and marked `[offscreen]` -- reachable by scrolling, which is
        exactly what an agent would do.
        """
        return [e for e in self.elements if not e.disabled]

    def to_text(self, actionable_only: bool = False) -> str:
        chosen = self.actionable if actionable_only else self.reachable
        header = f"{self.title} — {self.url}"
        return "\n".join([header] + [e.to_line() for e in chosen])

    @property
    def scene_chars(self) -> int:
        return len(self.to_text())

    @property
    def degenerate(self) -> bool:
        """True when the scene found nothing, so ratios are meaningless.

        A large page with an empty scene is a failure to perceive it, not a
        spectacular compression result. An earlier build of this file divided a
        56,000-character DOM by a 42-character empty scene and reported '1332x'.
        Guarding this is the difference between a benchmark and a brochure.
        """
        return not self.reachable and self.dom_chars > 0

    @property
    def reduction_ratio(self) -> float | None:
        """Measured for THIS page. None when the scene is degenerate."""
        if self.degenerate or not self.scene_chars:
            return None
        return round(self.dom_chars / self.scene_chars, 2)

    def find(self, text: str, role: str | None = None) -> Element | None:
        """Locate a control by what it says, not by a CSS path.

        Exact match first, then substring: 'Save' should not select 'Save and
        exit' when a control literally named 'Save' exists on the page.
        """
        needle = text.strip().lower()
        candidates = [e for e in self.actionable if role is None or e.role == role]
        for element in candidates:
            if element.name.strip().lower() == needle:
                return element
        for element in candidates:
            if needle in element.name.lower():
                return element
        return None

    def summary(self) -> str:
        roles: dict[str, int] = {}
        for element in self.reachable:
            roles[element.role] = roles.get(element.role, 0) + 1
        breakdown = ", ".join(f"{count} {role}" for role, count in sorted(roles.items()))
        ratio = "n/a (degenerate scene)" if self.reduction_ratio is None \
            else f"{self.reduction_ratio}x"
        return (f"{len(self.reachable)} reachable ({len(self.actionable)} on screen) "
                f"of {len(self.elements)} candidates ({breakdown}); "
                f"{self.dom_chars} DOM chars -> {self.scene_chars} scene chars, {ratio}")


def scene(tab: "Tab") -> Scene:
    """Extract the actionable scene from an attached tab."""
    raw = tab.evaluate(_COLLECT_JS)
    if not isinstance(raw, dict):
        raise TypeError(f"perception script returned {type(raw).__name__}, expected dict")
    elements = [
        Element(
            role=item.get("role", ""),
            name=item.get("name", ""),
            x=int(item.get("x", 0)),
            y=int(item.get("y", 0)),
            w=int(item.get("w", 0)),
            h=int(item.get("h", 0)),
            in_viewport=bool(item.get("in_viewport", False)),
            occluded=bool(item.get("occluded", False)),
            actionable=bool(item.get("actionable", False)),
            disabled=bool(item.get("disabled", False)),
            selector=item.get("selector", ""),
        )
        for item in raw.get("elements", [])
    ]
    return Scene(
        url=raw.get("url", ""),
        title=raw.get("title", ""),
        elements=elements,
        dom_chars=int(raw.get("dom_chars", 0)),
        element_count=int(raw.get("element_count", 0)),
        viewport=raw.get("viewport", {}) or {},
        scroll_y=int(raw.get("scroll_y", 0)),
    )
