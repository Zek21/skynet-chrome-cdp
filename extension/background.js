/**
 * Skynet Chrome CDP Connector — service worker.
 *
 * Pairs this browser profile with a local bridge. Three things here are not
 * obvious, and each one cost a real debugging round:
 *
 * 1. MANIFEST V3 SERVICE WORKERS DIE. Chrome terminates an idle service worker
 *    after roughly 30 seconds. A connector that opens a socket in a top-level
 *    `connect()` call and assumes it stays open will work in testing and then
 *    silently stop working minutes later, with the extension still showing as
 *    enabled. The `chrome.alarms` heartbeat below exists solely to survive that,
 *    and the connection is re-established on every wake rather than assumed.
 *
 * 2. THE PROFILE IS NOT HARDCODED. Earlier builds carried an allowlist of
 *    profile UUIDs, which meant the extension could not be shipped to a second
 *    machine without editing it — and, worse, that two hosts silently diverged
 *    into incompatible variants. The profile identity is generated once, stored
 *    in chrome.storage.local, and PRESENTED to the bridge at pairing time. The
 *    bridge decides whether to accept it. Identity is asserted by the browser
 *    and authorised by the bridge; neither is compiled in.
 *
 * 3. VERSION NEGOTIATION IS BIDIRECTIONAL. This half declares its own version
 *    AND the minimum bridge version it will talk to. If either side is too old
 *    the connection closes with a code that names the reason. There is no
 *    degraded mode, because the failure mode of a partial connection is commands
 *    that are accepted and never performed.
 */

const PROTOCOL_VERSION = "1.3.0";
const MIN_BRIDGE_VERSION = "1.3.0";
const DEFAULT_BRIDGE_PORT = 8502;
const HEARTBEAT_MINUTES = 0.5;

const CloseCode = {
  OK: 1000,
  MALFORMED_HELLO: 4400,
  UNAUTHORIZED: 4401,
  PROFILE_REFUSED: 4403,
  ORIGIN_REFUSED: 4409,
  VERSION_MISMATCH: 4426,
};

const state = {
  paired: false,
  bridgeVersion: null,
  lastError: null,
  lastContactAt: null,
};

/** Stable per-profile identity, generated here and authorised by the bridge. */
async function profileId() {
  const stored = await chrome.storage.local.get("skynet_profile_id");
  if (stored.skynet_profile_id) return stored.skynet_profile_id;
  const generated = crypto.randomUUID();
  await chrome.storage.local.set({ skynet_profile_id: generated });
  return generated;
}

async function bridgePort() {
  const stored = await chrome.storage.local.get("skynet_bridge_port");
  return stored.skynet_bridge_port || DEFAULT_BRIDGE_PORT;
}

async function storedToken() {
  const stored = await chrome.storage.local.get("skynet_token");
  return stored.skynet_token || null;
}

/** Numeric semver comparison. Lexical ordering puts 1.10.0 before 1.9.0. */
function compareVersions(a, b) {
  const pa = String(a).split(".").map(Number);
  const pb = String(b).split(".").map(Number);
  for (let i = 0; i < 3; i++) {
    const da = pa[i] || 0;
    const db = pb[i] || 0;
    if (da !== db) return da < db ? -1 : 1;
  }
  return 0;
}

/**
 * Pair with the bridge.
 *
 * The token is obtained from the bridge's bootstrap endpoint, which only answers
 * on loopback. It is stored in chrome.storage.local, never in the manifest and
 * never in source — a token in a shipped extension is a published token.
 */
async function pair() {
  const port = await bridgePort();
  const id = await profileId();
  const base = `http://127.0.0.1:${port}`;

  let token = await storedToken();
  if (!token) {
    const bootstrap = await fetch(`${base}/bootstrap-token`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: id, ext_version: PROTOCOL_VERSION }),
    });
    if (!bootstrap.ok) {
      throw new Error(`bootstrap refused: HTTP ${bootstrap.status}`);
    }
    const payload = await bootstrap.json();
    token = payload.token;
    if (!token) throw new Error("bootstrap returned no token");
    await chrome.storage.local.set({ skynet_token: token });
  }

  const hello = await fetch(`${base}/hello`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Skynet-Token": token,
    },
    body: JSON.stringify({
      profile_id: id,
      ext_version: PROTOCOL_VERSION,
      min_bridge_version: MIN_BRIDGE_VERSION,
    }),
  });

  if (hello.status === 401) {
    // The bridge restarted and minted a new secret. Drop ours and re-bootstrap
    // on the next heartbeat rather than retrying with a token that cannot work.
    await chrome.storage.local.remove("skynet_token");
    throw new Error(`unauthorized (${CloseCode.UNAUTHORIZED}); token discarded`);
  }
  if (!hello.ok) {
    throw new Error(`hello refused: HTTP ${hello.status}`);
  }

  const result = await hello.json();
  if (!result.accepted) {
    throw new Error(`bridge refused pairing: ${result.close_code} ${result.reason}`);
  }
  // The direction implementations forget: check the bridge is new enough for US.
  if (compareVersions(result.bridge_version, MIN_BRIDGE_VERSION) < 0) {
    throw new Error(
      `bridge ${result.bridge_version} is older than the minimum ` +
      `${MIN_BRIDGE_VERSION} (${CloseCode.VERSION_MISMATCH}); upgrade the bridge`
    );
  }

  state.paired = true;
  state.bridgeVersion = result.bridge_version;
  state.lastError = null;
  state.lastContactAt = new Date().toISOString();
  return result;
}

async function heartbeat() {
  try {
    await pair();
  } catch (error) {
    state.paired = false;
    state.lastError = String(error && error.message ? error.message : error);
  }
  await chrome.storage.local.set({ skynet_state: state });
}

// The service worker is terminated when idle, so pairing is re-established on
// every wake instead of being held open. An alarm is the only timer that
// survives termination; setInterval does not.
chrome.alarms.create("skynet-heartbeat", { periodInMinutes: HEARTBEAT_MINUTES });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "skynet-heartbeat") heartbeat();
});

chrome.runtime.onStartup.addListener(heartbeat);
chrome.runtime.onInstalled.addListener(heartbeat);

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "skynet:status") {
    sendResponse({
      ...state,
      ext_version: PROTOCOL_VERSION,
      min_bridge_version: MIN_BRIDGE_VERSION,
    });
    return true;
  }
  return false;
});

heartbeat();
