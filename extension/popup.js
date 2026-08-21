/** Reports real pairing state. A connector UI that always shows "ready" is worse
 *  than none: it converts an outage into a silent one. */
chrome.runtime.sendMessage({ type: "skynet:status" }, (status) => {
  const s = status || {};
  const paired = document.getElementById("paired");
  paired.textContent = s.paired ? "connected" : "not connected";
  paired.className = "v " + (s.paired ? "ok" : "bad");
  document.getElementById("ext").textContent = s.ext_version || "?";
  document.getElementById("bridge").textContent = s.bridgeVersion || "—";
  document.getElementById("seen").textContent = s.lastContactAt
    ? new Date(s.lastContactAt).toLocaleTimeString() : "never";
  document.getElementById("err").textContent = s.lastError || "";
});
