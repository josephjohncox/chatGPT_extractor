// Paste this entire file into DevTools on your signed-in https://chatgpt.com tab.
// Read-only requests: no chat creation, changes, external hosts, or body logging.
// Optionally download credentials as mise.local.toml after an explicit confirmation.
// The return value remains redacted; NEVER share the downloaded credential file.
(async () => {
  if (location.origin !== "https://chatgpt.com") {
    throw new Error("Run this only on https://chatgpt.com");
  }
  const report = { authentication: null, endpoints: {} };
  const sessionResponse = await fetch("/api/auth/session", { credentials: "include" });
  report.authentication = { status: sessionResponse.status };
  const session = await sessionResponse.json().catch(() => ({}));
  if (!sessionResponse.ok || typeof session.accessToken !== "string" || !session.accessToken) {
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
  if (confirm(
    "Download mise.local.toml containing your secret ChatGPT access token, workspace ID, and export output directory? " +
    "Keep this file private. It will not replace your repository's existing local config automatically."
  )) {
    const outputDirectory = prompt(
      "Export output directory (absolute path, ~/path, or repository-relative path). The browser will not access this directory:",
      "chatgpt_export"
    );
    if (outputDirectory === null) {
      report.credential_file = "cancelled";
      return report;
    }
    if (!outputDirectory.trim() || outputDirectory.includes("\u0000")) {
      report.credential_file = "invalid_output_directory";
      return report;
    }
    // JSON basic-string escaping is also valid TOML for these credential values.
    const tomlString = (value) => JSON.stringify(value).replace(
      /[\u007f-\u009f]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`
    );
    const toml = [
      "# PRIVATE: contains a ChatGPT bearer token. Do not commit or share.",
      "# If mise.local.toml already exists, replace only the listed keys under its [env].",
      "# Preserve unlisted settings and existing project overrides; do not add a second [env] table.",
      "# After saving in your repository: chmod 600 mise.local.toml",
      "[env]",
      `CHATGPT_ACCESS_TOKEN = ${tomlString(session.accessToken)}`,
      `CHATGPT_WORKSPACE_ID = ${tomlString(workspace)}`,
      "# Export/import destination and default source for the offline index.",
      "# Supports absolute paths, ~, and repository-relative paths under mise.",
      `CHATGPT_OUTPUT_DIR = ${tomlString(outputDirectory)}`,
      "# Build index.html and .chatgpt-index/chats/ automatically after export/import.",
      "# Set false (or pass --no-index) to disable automatic HTML generation.",
      'CHATGPT_BUILD_INDEX = "true"',
      "# Rebuild without exporting or authenticating: mise run index",
      "# Index another existing directory: mise run index -- /path/to/previous/export",
      "",
      "# The exporter discovers projects automatically, including pagination.",
      '# CHATGPT_AUTO_PROJECTS = "true"',
      "# Optional manual [project_id, folder_name] overrides/additions; preserve existing entries.",
      "# CHATGPT_PROJECTS = '[]'",
      "",
      "# Optional cookie authentication: copy manually from DevTools Application > Cookies.",
      "# HttpOnly cookies cannot be read by this script. Clear CHATGPT_ACCESS_TOKEN above to use cookies.",
      "# There is no automatic fallback from an expired bearer token to cookies.",
      '# CHATGPT_SESSION_TOKEN = ""    # Single cookie; otherwise use split .0/.1 below.',
      '# CHATGPT_SESSION_TOKEN_0 = ""  # Cookie ending .0 (or a single cookie).',
      '# CHATGPT_SESSION_TOKEN_1 = ""  # Cookie ending .1, if present.',
      "# See mise.local.toml.example and README.md for the full configuration reference.",
      "",
    ].join("\n");
    const url = URL.createObjectURL(new Blob([toml], { type: "text/plain;charset=utf-8" }));
    const link = document.createElement("a");
    try {
      link.href = url;
      link.download = "mise.local.toml";
      document.body.appendChild(link);
      link.click();
      report.credential_file = "download_requested";
    } catch {
      report.credential_file = "download_failed";
    } finally {
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  } else {
    report.credential_file = "declined";
  }
  return report;
})();
