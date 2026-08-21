"""Version negotiation, authentication, and the network boundary.

These are the rules that stop a bridge and an extension from half-working, and
that stop a web page from driving the user's signed-in browser. Each test names
the failure it prevents.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest

from skynet_chrome_cdp.handshake import (
    MIN_PEER_VERSION,
    PROTOCOL_VERSION,
    CloseCode,
    Version,
    assert_loopback,
    generate_token,
    negotiate,
    origin_allowed,
    read_token_file,
    token_file_is_private,
    verify_token,
    write_token_file,
)


class VersionTest(unittest.TestCase):
    def test_parses_semver(self):
        v = Version.parse("1.3.0")
        self.assertEqual((v.major, v.minor, v.patch), (1, 3, 0))

    def test_double_digit_minor_compares_numerically(self):
        """String comparison makes '1.10.0' < '1.9.0' and rejects a NEWER peer."""
        self.assertGreater(Version.parse("1.10.0"), Version.parse("1.9.0"))

    def test_prerelease_suffix_is_tolerated(self):
        self.assertEqual(Version.parse("2.0.0-beta.1"), Version.parse("2.0.0"))

    def test_garbage_raises(self):
        for bad in ("", "1.2", "v1.2.3", "latest", "1.2.3.4"):
            with self.assertRaises(ValueError, msg=bad):
                Version.parse(bad)


class NegotiationTest(unittest.TestCase):
    def test_matching_versions_are_accepted(self):
        result = negotiate(peer_version=PROTOCOL_VERSION, peer_min_version=MIN_PEER_VERSION)
        self.assertTrue(result.accepted)
        self.assertEqual(result.close_code, CloseCode.OK)

    def test_old_peer_is_refused_with_upgrade_required(self):
        result = negotiate(peer_version="1.0.11")
        self.assertFalse(result.accepted)
        self.assertEqual(result.close_code, CloseCode.VERSION_MISMATCH)
        self.assertIn("upgrade the extension", result.reason)

    def test_old_LOCAL_side_is_also_refused(self):
        """The direction implementations forget. A new extension talking to an
        old bridge is the silent half-broken state this module exists to stop:
        commands are accepted and never performed."""
        result = negotiate(peer_version="2.0.0", peer_min_version="2.0.0",
                           local_version="1.3.0")
        self.assertFalse(result.accepted)
        self.assertEqual(result.close_code, CloseCode.VERSION_MISMATCH)
        self.assertIn("upgrade the bridge", result.reason)

    def test_newer_peer_within_our_floor_is_accepted(self):
        result = negotiate(peer_version="1.9.0", peer_min_version="1.0.0")
        self.assertTrue(result.accepted)

    def test_malformed_hello_is_refused_not_defaulted(self):
        result = negotiate(peer_version="not-a-version")
        self.assertFalse(result.accepted)
        self.assertEqual(result.close_code, CloseCode.MALFORMED_HELLO)

    def test_unauthenticated_1_0_11_can_never_be_accepted(self):
        """1.3.0 made the token mandatory. Accepting an older peer 'for
        compatibility' would defeat the authentication entirely."""
        self.assertFalse(negotiate(peer_version="1.0.11").accepted)
        self.assertFalse(negotiate(peer_version="1.2.0").accepted)

    def test_result_serialises_for_the_wire(self):
        payload = negotiate(peer_version=PROTOCOL_VERSION).to_dict()
        self.assertEqual(
            set(payload),
            {"accepted", "close_code", "reason", "peer_version", "local_version", "features"},
        )


class TokenTest(unittest.TestCase):
    def test_tokens_are_unique_and_long(self):
        tokens = {generate_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_verify_accepts_only_the_exact_token(self):
        token = generate_token()
        self.assertTrue(verify_token(token, token))
        self.assertFalse(verify_token(token + "x", token))
        self.assertFalse(verify_token(token[:-1], token))

    def test_empty_or_missing_never_authenticates(self):
        """A bridge started without a token must refuse everyone, not everyone."""
        self.assertFalse(verify_token("", ""))
        self.assertFalse(verify_token(None, "secret"))
        self.assertFalse(verify_token("secret", None))
        self.assertFalse(verify_token("", "secret"))

    def test_token_file_is_private_on_this_platform(self):
        """Checks the property the PLATFORM enforces, not the POSIX bits.

        The original version of this test asserted the group/other mode bits were
        clear. That passes on Linux and is meaningless on Windows, where NTFS
        ignores mode bits and access comes from an inherited ACL -- so the token
        was writable as 0600 and still readable by whoever the parent directory
        granted. The test failing on Windows is what surfaced that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "token")
            token = generate_token()
            write_token_file(path, token)
            self.assertEqual(read_token_file(path), token)
            self.assertTrue(token_file_is_private(path),
                            "token file must not be readable by other users")

    @unittest.skipIf(sys.platform == "win32", "POSIX mode bits are inert on NTFS")
    def test_posix_mode_bits_are_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            write_token_file(path, generate_token())
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode & (stat.S_IRWXG | stat.S_IRWXO), 0)

    @unittest.skipUnless(sys.platform == "win32", "exercises the NTFS ACL path")
    def test_privacy_check_catches_a_grant_to_everyone(self):
        """Proves the Windows check can still FAIL. A checker that tolerates
        SYSTEM and Administrators could tolerate everything by accident; this
        grants a genuinely dangerous principal and requires a False."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            write_token_file(path, generate_token())
            self.assertTrue(token_file_is_private(path))
            granted = subprocess.run(["icacls", path, "/grant", "*S-1-1-0:R"],
                                     capture_output=True, text=True, timeout=15)
            if granted.returncode != 0:
                self.skipTest(f"could not modify ACL: {granted.stdout}{granted.stderr}")
            self.assertFalse(token_file_is_private(path),
                             "a world-readable token must not pass the privacy check")

    def test_privacy_check_is_false_for_a_missing_file(self):
        self.assertFalse(token_file_is_private(
            os.path.join(tempfile.gettempdir(), "definitely-not-here-xyz")))

    def test_missing_token_file_reads_as_none(self):
        self.assertIsNone(read_token_file(os.path.join(tempfile.gettempdir(), "nope-xyz")))


class BoundaryTest(unittest.TestCase):
    def test_loopback_addresses_are_allowed(self):
        for host in ("127.0.0.1", "::1", "localhost", "127.0.0.5"):
            self.assertEqual(assert_loopback(host), host)

    def test_wildcard_bind_is_refused(self):
        """0.0.0.0 publishes an authenticated handle on the user's signed-in
        browser to every device on the network."""
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.assertRaises(ValueError, msg=host):
                assert_loopback(host)

    def test_unresolvable_host_is_refused_rather_than_assumed_local(self):
        with self.assertRaises(ValueError):
            assert_loopback("evil.example.com")

    def test_origin_must_match_the_paired_extension(self):
        ext = "gdglaaoeegiffcknachiadloiejojoao"
        self.assertTrue(origin_allowed(f"chrome-extension://{ext}", ext))
        self.assertFalse(origin_allowed("https://evil.example.com", ext))
        self.assertFalse(origin_allowed(f"chrome-extension://{'z' * 32}", ext))

    def test_absent_origin_is_refused(self):
        """Any web page can POST to 127.0.0.1. No origin means no admission."""
        self.assertFalse(origin_allowed(None, "abc"))
        self.assertFalse(origin_allowed("", "abc"))
        self.assertFalse(origin_allowed("chrome-extension://abc", None))


if __name__ == "__main__":
    unittest.main()


class TokenWriteOrderTest(unittest.TestCase):
    """The secret must never exist on disk under un-hardened permissions.

    An earlier implementation wrote the token and THEN hardened the ACL, leaving
    a window in which the secret sat under whatever the parent directory granted
    -- inside a function whose docstring promises the opposite. It also ignored
    the return value of the hardening call, so a failure still handed back a path
    as though it were protected.
    """

    def test_the_file_is_hardened_before_the_secret_is_written(self):
        """Order is asserted on the source, because the window is not observable
        after the fact: by the time the call returns, both steps have run."""
        import inspect
        from skynet_chrome_cdp import handshake
        src = inspect.getsource(handshake.write_token_file)
        harden = src.index("_restrict_windows_acl")
        write = src.index("os.write(handle")
        self.assertLess(harden, write,
                        "the ACL must be established before the token is written")

    def test_a_failed_hardening_raises_and_leaves_no_file(self):
        from skynet_chrome_cdp import handshake
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            original = handshake._restrict_windows_acl
            handshake._restrict_windows_acl = lambda _p: False
            try:
                if sys.platform == "win32":
                    with self.assertRaises(PermissionError):
                        handshake.write_token_file(path, generate_token())
                    self.assertFalse(os.path.exists(path),
                                     "an unprotectable token file must not survive")
                else:
                    handshake.write_token_file(path, generate_token())
            finally:
                handshake._restrict_windows_acl = original

    def test_a_failed_privacy_verification_raises_and_leaves_no_file(self):
        from skynet_chrome_cdp import handshake
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "token")
            original = handshake.token_file_is_private
            handshake.token_file_is_private = lambda _p: False
            try:
                with self.assertRaises(PermissionError):
                    handshake.write_token_file(path, generate_token())
                self.assertFalse(os.path.exists(path))
            finally:
                handshake.token_file_is_private = original

    def test_the_happy_path_still_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "token")
            token = generate_token()
            written = write_token_file(path, token)
            self.assertEqual(read_token_file(written), token)
            self.assertTrue(token_file_is_private(written))
