// Paste this entire file into DevTools on your signed-in https://chatgpt.com tab.
// Read-only requests: no chat creation, changes, external hosts, or body logging.
// Optionally download credentials as mise.local.toml after an explicit confirmation.
// The return value remains redacted; NEVER share the downloaded credential file.
(async () => {
  if (location.origin !== "https://chatgpt.com") {
    throw new Error("Run this only on https://chatgpt.com");
  }
  const report = { authentication: null, endpoints: {} };
  const browserChallenge = response => response.headers.get("cf-mitigated")?.trim().toLowerCase() === "challenge";
  const sessionResponse = await fetch("/api/auth/session", { credentials: "include" });
  const session = await sessionResponse.json().catch(() => ({}));
  const hasAccessToken = sessionResponse.ok && !browserChallenge(sessionResponse) && typeof session.accessToken === "string" && session.accessToken.length > 0;
  report.authentication = {
    status: sessionResponse.status,
    browser_challenge: browserChallenge(sessionResponse),
    has_access_token: hasAccessToken,
  };
  if (!hasAccessToken) {
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
      report.endpoints[path] = { status: response.status, browser_challenge: browserChallenge(response), shape: shape(data) };
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
      "# Match the browser's User-Agent for CLI requests; this does not copy its full network context.",
      `CHATGPT_USER_AGENT = ${tomlString(navigator.userAgent)}`,
      "# Export/import destination and default source for the offline index.",
      "# Supports absolute paths, ~, and repository-relative paths under mise.",
      `CHATGPT_OUTPUT_DIR = ${tomlString(outputDirectory)}`,
      "# Build index.html and .chatgpt-index/chats/ automatically after export/import.",
      "# Set false (or pass --no-index) to disable automatic HTML generation.",
      'CHATGPT_BUILD_INDEX = "true"',
      "# Web exports snapshot available memories/instructions/settings by default (see auxiliary-status.json).",
      '# Disable with CHATGPT_AUXILIARY = "false" or --no-auxiliary; --auxiliary forces inclusion.',
      'CHATGPT_AUXILIARY = "true"',
      "# Download referenced attachments and reconstruct Canvas by default; --no-attachments opts out.",
      'CHATGPT_ATTACHMENTS = "true"',
      '# CHATGPT_MAX_ATTACHMENT_BYTES = "536870912"  # 512 MiB per file.',
      "# Backfill old raw JSON without re-exporting chats: mise run attachments -- /path/to/export",
      "# CLI progress is enabled by default: live terminal bars or timestamped redirected logs.",
      '# CHATGPT_PROGRESS = "true"  # Or pass --no-progress; results/errors are not suppressed.',
      "# Bounded retries: long server cooldowns defer the run with checkpoints retained (exit 75).",
      '# CHATGPT_MAX_RETRIES = "2"  # Per request; successful requests do not reset the shared sleep budget.',
      '# CHATGPT_MAX_RETRY_WAIT = "10"  # Seconds per pause; longer Retry-After is not shortened.',
      '# CHATGPT_MAX_TOTAL_RETRY_WAIT = "30"  # Seconds of retry sleep across metadata and files.',
      "# Rebuild without exporting or authenticating: mise run index",
      "# Index another existing directory: mise run index -- /path/to/previous/export",
      "",
      "# The exporter discovers projects automatically, including pagination.",
      '# CHATGPT_AUTO_PROJECTS = "true"',
      "# Optional manual [project_id, folder_name] overrides/additions; preserve existing entries.",
      "# CHATGPT_PROJECTS = '[]'",
      "",
      "# Optional cookie authentication: full steps are in docs/authentication.md.",
      "# 1. Sign in at https://chatgpt.com in your normal browser profile.",
      "# 2. Open DevTools: macOS Cmd+Option+I; Windows/Linux Ctrl+Shift+I or F12.",
      "# 3. Application > Cookies > https://chatgpt.com (Cookies may be under Storage).",
      "# 4. Filter for session-token; copy the complete Value, not the Name or Cookie header.",
      "# Single __Secure-next-auth.session-token -> CHATGPT_SESSION_TOKEN.",
      "# Split __Secure-next-auth.session-token.0 / .1 -> the matching _0 / _1 fields; do not join them.",
      "# HttpOnly cookies cannot be read by this script. Library downloads may need cookies alongside bearer auth.",
      "# Clear CHATGPT_ACCESS_TOKEN only to force cookie-based API exchange; it may stay set for library downloads.",
      "# Also clear unused cookie fields: the single-cookie field takes precedence over split parts.",
      "# There is no automatic fallback from an expired bearer token to cookies.",
      '# CHATGPT_SESSION_TOKEN = ""    # Single cookie; otherwise use split .0/.1 below.',
      '# CHATGPT_SESSION_TOKEN_0 = ""  # Cookie ending .0 (or a single cookie).',
      '# CHATGPT_SESSION_TOKEN_1 = ""  # Cookie ending .1, if present.',
      "# Save the local file, run chmod 600 mise.local.toml, then mise run probe (no bulk export).",
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
