"""Version negotiation and authentication between the bridge and the extension.

WHY THIS MODULE EXISTS
----------------------
This connector has two halves that ship on different clocks: a Python bridge the
user updates with git or pip, and a browser extension Chrome may update on its
own schedule. When the halves drift, the failure is not a clean error. It is a
command that is accepted, acknowledged, and silently not performed -- which is
the worst possible failure for automation, because the caller records success.

The fix is that the two halves refuse to talk until they have agreed they can.
Negotiation is bidirectional: each side declares its own version AND the minimum
version it can tolerate in the other. Either side may refuse.

    extension --> bridge : ext_version, min_bridge_version
    bridge    --> extension : bridge_version, min_ext_version, features

If either minimum is unmet the connection is closed with a code that names the
reason. No partial state, no degraded mode.

THREAT MODEL
------------
The DevTools port and this bridge both grant full authority over a signed-in
browser profile: every cookie, every session, every open tab. Two consequences
are treated as hard requirements rather than options:

  1. Loopback only. `assert_loopback()` refuses any bind address that is not
     127.0.0.1/::1. Binding 0.0.0.0 publishes the user's logged-in browser to
     the local network.

  2. Authenticated by default. Any page in the browser can issue requests to
     127.0.0.1. Without a shared secret, a malicious site can drive the bridge
     that drives the user's browser. The token is generated per run, never
     committed, and compared in constant time.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import stat
import subprocess
import sys
from dataclasses import dataclass, field

__all__ = [
    "PROTOCOL_VERSION", "CloseCode", "Version", "Negotiation",
    "negotiate", "generate_token", "verify_token", "write_token_file",
    "read_token_file", "token_file_is_private", "assert_loopback", "origin_allowed",
]

# The connector protocol this build speaks. Distinct from the package version:
# a package release that does not change the wire contract does not change this.
PROTOCOL_VERSION = "1.3.0"

# Minimums this build accepts. 1.3.0 is the first version in which the token is
# mandatory, so anything older is refused rather than downgraded: accepting an
# unauthenticated peer "for compatibility" would defeat the authentication.
MIN_PEER_VERSION = "1.3.0"

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")

# Windows principals this check deliberately TOLERATES on the ACL.
#
# Not "unavoidable" -- an earlier version of this comment said so and was wrong.
# A DACL can be written without an Administrators ACE, and `icacls /inheritance:r`
# plus a single grant will produce one. The reason to tolerate them is different
# and narrower: a local administrator can take ownership of any file and rewrite
# its DACL, so removing their ACE does not deny them the token. It changes what
# the audit log looks like, not who can read the secret.
#
# Refusing to tolerate them would therefore fail the check on a correctly
# protected file while buying no protection. What the check MUST still catch is a
# grant to an ordinary principal -- Users, Everyone, Authenticated Users, or
# another user account -- which is exactly what the Everyone:R test asserts.
_WINDOWS_TOLERATED_PRINCIPALS = frozenset({
    "nt authority\\system",
    "builtin\\administrators",
    "owner rights",
})


class CloseCode:
    """WebSocket close codes in the private 4000-4999 range (RFC 6455 §7.4.2).

    Named codes, because `1006 abnormal closure` in a log tells a maintainer
    nothing about which of these five situations occurred.
    """

    OK = 1000
    VERSION_MISMATCH = 4426       # mirrors HTTP 426 Upgrade Required
    UNAUTHORIZED = 4401           # missing or wrong token
    PROFILE_REFUSED = 4403        # profile not paired with this bridge
    MALFORMED_HELLO = 4400        # hello frame unparseable
    ORIGIN_REFUSED = 4409         # request origin is not the paired extension


@dataclass(frozen=True, order=True)
class Version:
    """A semantic version that compares correctly.

    String comparison makes "1.10.0" < "1.9.0", which silently rejects a newer
    peer. Parsing to integers is the whole point of this type.
    """

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        match = _SEMVER.match(str(text).strip())
        if not match:
            raise ValueError(f"not a semantic version: {text!r}")
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass
class Negotiation:
    """The outcome. `accepted` is the only field a caller may act on."""

    accepted: bool
    close_code: int = CloseCode.OK
    reason: str = ""
    peer_version: str = ""
    local_version: str = PROTOCOL_VERSION
    features: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "close_code": self.close_code,
            "reason": self.reason,
            "peer_version": self.peer_version,
            "local_version": self.local_version,
            "features": list(self.features),
        }


def negotiate(peer_version: str,
              peer_min_version: str | None = None,
              local_version: str = PROTOCOL_VERSION,
              min_peer_version: str = MIN_PEER_VERSION,
              features: list[str] | None = None) -> Negotiation:
    """Decide whether these two halves may talk to each other.

    Refuses in three cases, each with a distinct close code:
      * either version string is unparseable  -> MALFORMED_HELLO
      * the peer is older than we accept      -> VERSION_MISMATCH
      * we are older than the peer accepts    -> VERSION_MISMATCH

    The third case is the one implementations usually omit, and it is the one
    that produces the silent half-broken state: a new extension talking to an old
    bridge which accepts commands it does not implement.
    """
    try:
        peer = Version.parse(peer_version)
        local = Version.parse(local_version)
        floor = Version.parse(min_peer_version)
    except ValueError as exc:
        return Negotiation(False, CloseCode.MALFORMED_HELLO, str(exc),
                           peer_version=str(peer_version), local_version=local_version)

    if peer < floor:
        return Negotiation(
            False, CloseCode.VERSION_MISMATCH,
            f"peer {peer} is older than the minimum {floor}; upgrade the extension",
            peer_version=str(peer), local_version=str(local))

    if peer_min_version:
        try:
            peer_floor = Version.parse(peer_min_version)
        except ValueError as exc:
            return Negotiation(False, CloseCode.MALFORMED_HELLO, str(exc),
                               peer_version=str(peer), local_version=str(local))
        if local < peer_floor:
            return Negotiation(
                False, CloseCode.VERSION_MISMATCH,
                f"this bridge {local} is older than the peer's minimum "
                f"{peer_floor}; upgrade the bridge",
                peer_version=str(peer), local_version=str(local))

    return Negotiation(True, CloseCode.OK, "ok", peer_version=str(peer),
                       local_version=str(local), features=list(features or []))


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def generate_token(nbytes: int = 32) -> str:
    """A fresh per-run secret. `secrets`, never `random`."""
    return secrets.token_urlsafe(nbytes)


def verify_token(presented: str | None, expected: str | None) -> bool:
    """Constant-time comparison.

    `==` on secrets leaks length and prefix through timing. The bridge is on
    loopback where timing is measurable with little noise, so this matters more
    here than it would across a network.
    """
    if not presented or not expected:
        return False
    return hmac.compare_digest(str(presented), str(expected))


def write_token_file(path: str, token: str) -> str:
    """Persist the token so only the current user can read it.

    Written 0600 at creation rather than chmod-ed afterwards: a secret that is
    world-readable for even a moment on a shared machine has been disclosed.

    WINDOWS IS NOT POSIX HERE. `os.open(..., S_IRUSR|S_IWUSR)` looks like it
    restricts the file, and `os.stat().st_mode` will even report something
    plausible, but READ access on NTFS is decided by an ACL the file INHERITS
    from its parent directory, not by those bits. (They are not discarded
    outright -- the write bit maps onto the read-only attribute -- but nothing in
    them yields owner-only read semantics, which is the whole reason to write
    0600 for a secret.) A token written this way in a shared or roamed folder is
    readable by anyone the parent grants. This was caught by a test asserting the
    group/other bits were clear; on Windows they were not.

    So on Windows the inherited ACL is stripped and a single explicit grant to
    the current user is applied.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    handle = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(handle, token.encode("utf-8"))
    finally:
        os.close(handle)

    absolute = os.path.abspath(path)
    if sys.platform == "win32":
        _restrict_windows_acl(absolute)
    return absolute


def _current_windows_principal() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain and user else (user or "")


def _restrict_windows_acl(path: str) -> bool:
    """Remove inherited access and grant only the current user. Best effort."""
    principal = _current_windows_principal()
    if not principal:
        return False
    try:
        completed = subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True, text=True, timeout=15,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def token_file_is_private(path: str) -> bool:
    """Verify the stored token is not readable by other users, per platform.

    Exists because the POSIX-only assertion passed on Linux and quietly failed to
    describe Windows at all. Checking the property the platform actually enforces
    is the only version of this check worth having.
    """
    if not os.path.exists(path):
        return False
    if sys.platform != "win32":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        return not mode & (stat.S_IRWXG | stat.S_IRWXO)
    try:
        completed = subprocess.run(["icacls", path], capture_output=True,
                                   text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    principal = _current_windows_principal().lower()
    user_only = principal.split("\\")[-1]
    for line in completed.stdout.splitlines():
        entry = line.strip()
        if not entry or "Successfully processed" in entry:
            continue
        # icacls prints the path on the first line, followed by the first ACE.
        if entry.lower().startswith(path.lower()):
            entry = entry[len(path):].strip()
        if not entry or ":" not in entry:
            continue
        grantee = entry.split(":", 1)[0].strip().lower()
        if not grantee:
            continue
        if grantee == principal or grantee.split("\\")[-1] == user_only:
            continue
        if grantee in _WINDOWS_TOLERATED_PRINCIPALS:
            continue
        return False
    return True


def read_token_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Network boundary
# ---------------------------------------------------------------------------
def assert_loopback(host: str) -> str:
    """Refuse any bind address that is not loopback.

    This is a hard refusal rather than a warning. A bridge on 0.0.0.0 hands
    every device on the network authenticated control of the user's signed-in
    browser; there is no configuration in which that is the intent.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host.lower() in ("localhost", "localhost.localdomain"):
            return host
        raise ValueError(f"refusing non-loopback bind host {host!r}") from None
    if not address.is_loopback:
        raise ValueError(
            f"refusing to bind {host}: the bridge controls a signed-in browser "
            f"and must listen on loopback only"
        )
    return host


def origin_allowed(origin: str | None, extension_id: str | None) -> bool:
    """Accept only the paired extension's own origin.

    Any web page can send a request to 127.0.0.1. Without this check a visited
    site could reach the bridge; with it, requests must originate from the
    chrome-extension:// origin that completed pairing.
    """
    if not origin or not extension_id:
        return False
    return origin.strip().lower() == f"chrome-extension://{extension_id}".lower()
