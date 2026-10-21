# API and live UI investigation

Checked on 2026-10-21. Separate documented API behavior, community protocol evidence, offline test results, and live account validation.

## Supported OpenAI APIs are not the ChatGPT web history backend

The documented [Conversations API](https://developers.openai.com/api/docs/guides/conversation-state) works with the Responses API to store API conversation state. Its [list-items method](https://developers.openai.com/api/reference/resources/conversations/subresources/items/methods/list) requires a known API conversation ID and paginates items using `after` / `has_more`. These are not the ChatGPT web application's conversation-list endpoints or IDs. No supported endpoint for enumerating a user's ChatGPT web history was found in the reviewed API documentation.

Consequently this exporter does not pretend that an OpenAI API key can export ChatGPT web history, and it does not create API conversations or send messages. Adding a separate exporter for API-created conversations would require known API conversation IDs and its own credentials/schema.

OpenAI's [data-export instructions](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data) document the supported offline source: a downloaded ZIP containing chat history and account data. Self-service exports are available for Free, Go, Plus, Pro, and eligible Edu workspaces, but not Business, Enterprise, or Healthcare workspaces. The offline importer accepts `conversations.json`, a single raw conversation JSON, or a ZIP with exactly one `conversations.json`. ZIP members are read in place, never extracted to paths supplied by the archive.

## ChatGPT web endpoint evidence

These endpoints are **undocumented** and may change by account, workspace, or rollout:

| Purpose | Endpoint | Implementation |
|---|---|---|
| Session exchange | `/api/auth/session` | Single or split cookie → `accessToken`; no response-body logging |
| Regular/archived listing | `/backend-api/conversations` | Offset pagination; `limit`, `order=updated`, optional archived listing |
| Project discovery | `/backend-api/gizmos/snorlax/sidebar` | Cursor pagination; nested `gizmo.gizmo` / `gizmo` shapes; `g-p-` IDs only |
| Project conversations | `/backend-api/gizmos/{id}/conversations` | Initial cursor `0`, then returned cursors |
| Conversation detail | `/backend-api/conversation/{id}` | Raw mapping retained; Markdown uses selected-node ancestry |
| User settings | `/backend-api/settings/user` | Default web-export snapshot (can be disabled); replaces the original guessed `/settings` path |
| Other auxiliary data | memories, user system messages, model/account metadata, beta features, Codex usage, compliance | Attempted by default in web exports; availability recorded per endpoint, not assumed supported or complete |

Community evidence for project listing shapes and pagination:

- [brianjlacy/export-chatgpt project API research](https://github.com/brianjlacy/export-chatgpt/blob/main/_docs/PROJECTS_FILES_RESEARCH.md): nested sidebar payload, cursor pagination, project conversation membership, workspace header, and file references.
- [PZEHONG/ChatGPT-API protocol reference](https://github.com/PZEHONG/ChatGPT-API/blob/master/docs/protocol-reference.md): session exchange, regular conversation mapping, sidebar discovery, and `/settings/user`.

These sources are implementation leads, not guarantees or live evidence for this account. File-download and message-sending endpoints described there were not implemented or exercised.

## Actual live observations

1. A fetched public `https://chatgpt.com/` page displayed the logged-out UI, with New chat, Images, Settings, and login choices. This is not evidence about the authenticated project sidebar.
2. `mise run probe` used the existing locally embedded cookies after migration into `mise.local.toml`. `/api/auth/session` returned **HTTP 403**. The tool stopped before querying conversation content. Response bodies and token values were not logged.
3. A one-off Playwright check launched installed Google Chrome in a new isolated, headless context with those same cookies. `GET https://chatgpt.com/` returned **403**, the page title was **“Just a moment…”**, and an in-page request to `/api/auth/session` also returned **403** with no usable access token. The browser context was closed; no browser profile was scraped or changed.

A subsequent session-only repro returned 403 twice. A credential-safe inspection confirmed that the selected request uses both `.0` and `.1` cookies, no single-cookie override, and no workspace header. The response carries **`cf-mitigated: challenge`** and HTML content rather than JSON. This positively identifies a Cloudflare browser challenge on the Python session request; it does not determine whether the cookie values would be accepted after that challenge. The CLI now detects this explicit header without printing response bodies, and the browser helper reports a `browser_challenge` flag plus token-presence status.

An unclassified HTTP 403 alone cannot distinguish expired credentials from access restrictions or browser gating. The isolated Chrome result does not prove the user's normal browser is blocked. These initial observations did not establish the cause of the challenge. They were superseded by the controlled User-Agent comparison below. No content was generated, changed, or deleted in ChatGPT, and no bulk download was started.

### Controlled User-Agent comparison and correction

Using the same endpoint, credentials, and request headers except User-Agent, a three-request comparison produced:

| User-Agent | HTTP status | Challenge header |
|---|---|---|
| Generic `Mozilla/5.0` | 403 | present |
| Desktop Chrome string matching the installed Chrome version | 200 | absent |
| Generic `Mozilla/5.0` again | 403 | present |

The generic header was therefore a sufficient trigger in this comparison. It was premature to conclude that a browser-context exporter was necessarily required. The client now defaults to the tested desktop Chrome string and exposes `CHATGPT_USER_AGENT`; the browser helper copies `navigator.userAgent` automatically. This changes only a request header, not TLS behavior or the rest of the transport.

After the correction, normal `mise run probe` returned 200 for conversations, project sidebar, and settings in configured-bearer mode. A separate probe with the bearer token removed in memory (without modifying the private file) completed split-cookie session exchange and returned 200 for the same three backend endpoints. Full sidebar cursor pagination also completed, and one conversation-detail request returned a mapping and current node. No bulk conversation export was started. One earlier project-listing trial was still challenged before subsequent successful checks; this is not a universal or permanent guarantee.

## Local verification

`mise run check` runs Ruff, format checks, mypy, and offline tests. Tests cover credentials/configuration, both pagination schemes, repeated/broken pages, project discovery shapes, deduplication, active-branch rendering, timestamp variants, duplicate titles, checksum/update-aware reruns, safe/atomic private output, JSON/ZIP import, optional unavailable snapshots, and nonzero failure status. All test transports are controlled and actual network calls are forbidden. Passing these tests does not prove live endpoint compatibility.

## Remaining live gate

Run `scripts/probe-chatgpt.js` in DevTools on the user's normal signed-in `https://chatgpt.com` tab. Its console report contains only statuses and response shapes. Supply the active workspace account ID if needed. After the checks, an explicit confirmation can download `mise.local.toml` containing the browser's bearer token, the supplied workspace ID, the browser's `navigator.userAgent` as `CHATGPT_USER_AGENT`, a prompted export output directory, and explicit `CHATGPT_BUILD_INDEX` / `CHATGPT_AUXILIARY` settings with usage comments; this credential file is separate from the redacted report and must stay private. Merge its six active keys into an existing local file, preserving other settings and intentional index-generation or auxiliary-snapshot preferences, then set permissions to 0600. Compare the project nesting/pagination fields with the adapter and add sanitized fixtures for any new shapes. Do not share tokens, cookies, session JSON, raw HAR files, or conversation bodies.

Keep the configured User-Agent aligned with the signed-in browser, rerun `mise run probe`, then perform a deliberately scoped export to a separate ignored output directory before a full account export. If challenges persist, compare one request variable at a time; a separate browser-context adapter is not implemented and should not be assumed necessary from a challenge response alone. Refreshing tokens without isolating the request difference is not proof of a fix.

**Confidence:** high that changing the generic User-Agent resolved the observed live session/backend failure in the controlled comparison, with successful bearer and cookie-only probes, sidebar pagination, and one detail request. Full bulk-export completeness, all workspace/plan variants, and future challenge behavior remain unqualified.
