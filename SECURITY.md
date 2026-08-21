# Security model

Read this before pointing this library at a browser you care about.

## What you are actually enabling

Starting Chrome with `--remote-debugging-port=9222` grants **anything that can
open a TCP connection to that port** the ability to:

- read every cookie in the profile, including session cookies for mail, banking,
  and cloud consoles;
- read and modify any page, including pages behind authentication;
- execute arbitrary JavaScript in any origin the browser has loaded;
- observe every network request the browser makes.

There is no per-site permission model on that port and no authentication in
front of it: the capabilities listed above are available to any local process
that can connect. Scope them by choosing which profile sits behind the port,
because nothing else will.

This is not a defect in this library or in Chrome — it is what the DevTools
Protocol is for. It matters here because a session-attached connector is, by
definition, pointed at a browser that has something worth protecting in it.

**Chrome enforces part of this for you now.** Since version 136,
`--remote-debugging-port` is ignored when it would open the default user data
directory; it takes effect only with a `--user-data-dir` pointing at a
non-standard location. A non-default directory is encrypted with a different key,
so the passwords and cookies in the user's real profile stay out of the
protocol's reach. Launched without it, Chrome creates no listening socket and no
`DevToolsActivePort` file.

Read that restriction accurately: it limits *what an attacker finds*, not *who
can connect*. The port itself still has no authentication. Anything that can
reach it retains full control of whatever profile is behind it — and a
non-default directory can hold a fully signed-in profile.

## Rules this library enforces

**Loopback only.** `assert_loopback()` raises on any bind address that is not
`127.0.0.1` / `::1`. It does not warn and proceed. Chrome itself binds the
DevTools port to loopback by default; do not defeat that with a port forward, an
SSH tunnel you leave open, or `--remote-debugging-address=0.0.0.0`.

**Authenticated by default.** Any page open in the browser can issue requests to
`127.0.0.1`. Without a shared secret, a website you visit can send commands to
the bridge that controls your browser — a full session takeover initiated by
loading a page. Requests carry a per-run token compared with
`hmac.compare_digest`.

**Origin-bound.** The bridge accepts only the paired extension's
`chrome-extension://` origin.

**Ownership-guarded.** Tabs the connector did not create are read-only unless the
caller passes `allow_foreign=True` on that specific call.

**No degraded mode.** Version mismatch, bad token, wrong origin, unparseable
hello — all close the connection with a code that names the reason. A security
check that fails open is worse than no check, because it produces a log line
saying everything was fine.

## Storing the token

The token is written with owner-only access and then **verified** with
`token_file_is_private()`.

The verification exists because the obvious implementation is wrong on Windows.
`os.open(path, ..., S_IRUSR | S_IWUSR)` looks like it restricts the file, and
`os.stat().st_mode` reports something plausible — but NTFS ignores POSIX mode
bits entirely. Access is decided by an ACL the file **inherits from its parent
directory**. A token written "0600" into a shared, roamed, or synced folder is
readable by everyone that folder grants.

So on Windows the inherited ACL is stripped (`icacls /inheritance:r`) and a single
explicit grant is applied to the current user. `SYSTEM`, `Administrators` and `OWNER RIGHTS` are tolerated on the resulting
ACL. Not because they cannot be removed — they can — but because a local
administrator can take ownership of any file and rewrite its DACL anyway.
Stripping their ACE does change who may read the file directly — but it cannot
prevent a sufficiently privileged local administrator from ultimately obtaining
access, since they can take ownership and rewrite the DACL. It would, meanwhile,
cause the check to fail on files that are in fact correctly protected.

This was found by a test, not by review. The test asserted the POSIX group/other
bits were clear; it passed on Linux and failed on Windows, where those bits do
not control read access at all.

## Recommended configuration

Use a **dedicated profile** for automation:

```bash
chrome --remote-debugging-port=9222 --user-data-dir="%TEMP%\chrome-automation"
```

Sign that profile into only the accounts the automation needs. The convenience of
attaching to your daily browser is real; so is the blast radius of a mistake in
a script that has access to it.

If you do attach to a daily profile:

- do not run untrusted automation code against it;
- close the debugging port when you are done — it does not close itself;
- remember that browser extensions in that profile also run with the profile's
  authority.

## What is deliberately absent

No proxy rotation, no fingerprint spoofing, no CAPTCHA solving, no detection
evasion. This project attaches to a session a human established; it does not help
anyone pretend to be a human they are not.

## Reporting

Open an issue at https://github.com/Zek21/skynet-chrome-cdp/issues. For anything
you believe is exploitable, mark it clearly and omit a working exploit from the
public description.
