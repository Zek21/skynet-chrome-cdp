"""Skynet Chrome CDP — attach to the Chrome the user is already signed into.

    from skynet_chrome_cdp import Chrome, scene

    with Chrome(port=9222) as chrome:
        tab = chrome.new_tab("https://example.com")   # a tab we own
        with tab:
            print(scene(tab).to_text())               # what can be acted on
        chrome.close_tab(tab)

Zero third-party dependencies, on purpose: the machines where browser automation
is most needed are often the ones where you cannot install anything.
"""
from .cdp import CDPError, Chrome, Tab, TabOwnershipError
from .handshake import (
    PROTOCOL_VERSION,
    CloseCode,
    Negotiation,
    Version,
    assert_loopback,
    generate_token,
    negotiate,
    origin_allowed,
    verify_token,
)
from .perception import ACTIONABLE_ROLES, Element, Scene, scene

__version__ = "1.3.0"
__all__ = [
    "Chrome", "Tab", "CDPError", "TabOwnershipError",
    "scene", "Scene", "Element", "ACTIONABLE_ROLES",
    "negotiate", "Negotiation", "Version", "CloseCode", "PROTOCOL_VERSION",
    "generate_token", "verify_token", "assert_loopback", "origin_allowed",
    "__version__",
]
