// Paste this entire file into DevTools on your signed-in https://chatgpt.com tab.
// Read-only: no chat creation, changes, downloads, external hosts, or body logging.
// The return value contains status codes, field names, and counts only.
(async () => {
  if (location.origin !== "https://chatgpt.com") {
    throw new Error("Run this only on https://chatgpt.com");
  }
  const report = { authentication: null, endpoints: {} };
  const sessionResponse = await fetch("/api/auth/session", { credentials: "include" });
  report.authentication = { status: sessionResponse.status };
  const session = await sessionResponse.json().catch(() => ({}));
  if (!sessionResponse.ok || typeof session.accessToken !== "string") {
    return report;
  }
  const workspace = prompt("ChatGPT-Account-ID for the workspace to check (blank for personal/default):", "");
  if (workspace === null) return { cancelled: true };
  if (workspace && !/^[a-zA-Z0-9_-]+$/.test(workspace)) {
    throw new Error("Expected an account ID, not a URL or header");
  }
  const headers = { Authorization: `Bearer ${session.accessToken}` };
  if (workspace) headers["ChatGPT-Account-ID"] = workspace;
  function shape(value, depth = 0) {
    if (value === null) return "null";
    if (Array.isArray(value)) return { type: "array", count: value.length, first: value.length && depth < 4 ? shape(value[0], depth + 1) : null };
    if (typeof value === "object") return depth < 4
      ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, shape(item, depth + 1)]))
      : { type: "object", keys: Object.keys(value).sort() };
    return typeof value;
  }
  for (const path of [
    "conversations?offset=0&limit=1&order=updated",
    "gizmos/snorlax/sidebar?conversations_per_gizmo=0&owned_only=false",
    "settings/user",
  ]) {
    try {
      const response = await fetch(`/backend-api/${path}`, { headers, credentials: "include", redirect: "error" });
      const data = await response.json().catch(() => null);
      report.endpoints[path] = { status: response.status, shape: shape(data) };
    } catch {
      report.endpoints[path] = { error: "Request failed (details withheld)" };
    }
  }
  return report;
})();
