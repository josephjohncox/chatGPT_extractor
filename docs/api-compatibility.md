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
| User settings | `/backend-api/settings/user` | Optional snapshot; replaces the original guessed `/settings` path |
| Other auxiliary data | memories, user system messages, model/account metadata, beta features, Codex usage, compliance | Optional availability checks only; not assumed supported |

Community evidence for project listing shapes and pagination:

- [brianjlacy/export-chatgpt project API research](https://github.com/brianjlacy/export-chatgpt/blob/main/_docs/PROJECTS_FILES_RESEARCH.md): nested sidebar payload, cursor pagination, project conversation membership, workspace header, and file references.
- [PZEHONG/ChatGPT-API protocol reference](https://github.com/PZEHONG/ChatGPT-API/blob/master/docs/protocol-reference.md): session exchange, regular conversation mapping, sidebar discovery, and `/settings/user`.

These sources are implementation leads, not guarantees or live evidence for this account. File-download and message-sending endpoints described there were not implemented or exercised.

## Actual live observations

1. A fetched public `https://chatgpt.com/` page displayed the logged-out UI, with New chat, Images, Settings, and login choices. This is not evidence about the authenticated project sidebar.
2. `mise run probe` used the existing locally embedded cookies after migration into `mise.local.toml`. `/api/auth/session` returned **HTTP 403**. The tool stopped before querying conversation content. Response bodies and token values were not logged.
3. A one-off Playwright check launched installed Google Chrome in a new isolated, headless context with those same cookies. `GET https://chatgpt.com/` returned **403**, the page title was **“Just a moment…”**, and an in-page request to `/api/auth/session` also returned **403** with no usable access token. The browser context was closed; no browser profile was scraped or changed.

The HTTP result alone cannot distinguish expired credentials from access restrictions or browser/Cloudflare gating. The Chrome challenge confirms gating in that isolated context, not that the user's normal browser is blocked. No authenticated UI, project listing, conversation-detail response, or full live export has been validated. No content was generated, changed, or deleted in ChatGPT, and no bulk download was started.

## Local verification

`mise run check` runs Ruff, format checks, mypy, and offline tests. Tests cover credentials/configuration, both pagination schemes, repeated/broken pages, project discovery shapes, deduplication, active-branch rendering, timestamp variants, duplicate titles, checksum/update-aware reruns, safe/atomic private output, JSON/ZIP import, optional unavailable snapshots, and nonzero failure status. All test transports are controlled and actual network calls are forbidden. Passing these tests does not prove live endpoint compatibility.

## Remaining live gate

Run `scripts/probe-chatgpt.js` in DevTools on the user's normal signed-in `https://chatgpt.com` tab. Its console report contains only statuses and response shapes. Supply the active workspace account ID if needed. After the checks, an explicit confirmation can download `mise.local.toml` containing the browser's bearer token, the supplied workspace ID, a prompted export output directory, and an explicit `CHATGPT_BUILD_INDEX` setting with usage comments; this credential file is separate from the redacted report and must stay private. Merge its four active keys into an existing local file, preserving other settings and any intentional index-generation preference, then set permissions to 0600. Compare the project nesting/pagination fields with the adapter and add sanitized fixtures for any new shapes. Do not share tokens, cookies, session JSON, raw HAR files, or conversation bodies.

If a fresh web bearer token works outside the browser, store it only in `mise.local.toml`, rerun `mise run probe`, then perform a deliberately scoped export to a separate ignored output directory. If browser gating persists, use a downloaded official export where available; a browser-side export adapter remains future work. Refreshing tokens or adding speculative headers is not proof of compatibility.

**Confidence:** high in the recorded 403/challenge observations and offline behavior covered by tests; provisional in web endpoint compatibility until an authenticated live probe and scoped export pass.
