# ChatGPT Conversation Exporter

Private derivative of [richlow/chatGPT_extractor](https://github.com/richlow/chatGPT_extractor), retaining the upstream Git history and MIT license. GitHub does not support making a public repository's fork private, so this is an independent private repository, not a GitHub fork-network member.

Snapshot ChatGPT conversations, referenced attachments/images/artifacts, and available memories, custom instructions, and account/settings data by default. Conversations are raw JSON (default) or Markdown, grouped by project; auxiliary snapshots remain JSON. See [default snapshot contents and opt-out](docs/snapshots.md). Or convert a downloaded ChatGPT data export **offline**, without credentials or network access.

**Live checks now pass with a desktop-browser User-Agent:** session exchange, regular listing, full project-sidebar pagination, user settings, and one conversation-detail response were verified. The earlier generic `Mozilla/5.0` header received Cloudflare challenges. Full bulk-export coverage and universal plan support are not claimed; these endpoints remain undocumented. See [API and UI investigation](docs/api-compatibility.md).

## Setup: mise + uv

Install [mise](https://mise.jdx.dev/), then:

```sh
git clone git@github.com:josephjohncox/chatGPT_extractor.git
cd chatGPT_extractor
mise trust mise.toml
mise install --locked
mise run deps
```

The tool versions are exact pins in `mise.toml`; `mise.lock` includes artifact checksums. `uv.lock` locks Python dependencies. As in the mise data template, mise selects tools and exposes thin tasks, while uv owns `.venv`. There is no manual environment activation and no global tool configuration to change.

## Credentials: private local mise file

For web mode only:

```sh
# Only on a fresh clone; do not overwrite an existing local credential file.
cp -n mise.local.toml.example mise.local.toml
chmod 600 mise.local.toml
# Edit mise.local.toml in your editor, then:
mise trust mise.local.toml
```

Mise automatically merges `mise.local.toml`; Python reads only environment variables. The private file is Git-ignored. Never put credentials in Python source, command-line arguments, issues, or committed mise configuration. Do not use verbose HTTP/environment debugging with real credentials.

Follow **[Get ChatGPT session tokens](docs/authentication.md)** for numbered browser steps, single/split-cookie mapping, workspace selection, and an authentication check. The guide includes Chrome/Edge shortcuts and the Firefox cookie-view location.

Choose one authentication method in that file:

- **Bearer token:** in a signed-in browser, open `https://chatgpt.com/api/auth/session` and place its `accessToken` value in `CHATGPT_ACCESS_TOKEN`.
- **Single session cookie:** copy `__Secure-next-auth.session-token` from browser DevTools → Application → Cookies → `https://chatgpt.com` into `CHATGPT_SESSION_TOKEN`.
- **Split cookies:** copy `.0` and `.1` values into `CHATGPT_SESSION_TOKEN_0` and `CHATGPT_SESSION_TOKEN_1`. A single cookie in `_0` is also accepted for compatibility.

Bearer authentication takes precedence; a single cookie takes precedence over split cookies. Tokens expire. There is no automatic fallback from an expired bearer token: rerun the helper to refresh it, or clear `CHATGPT_ACCESS_TOKEN` to use cookie-based session exchange. An OpenAI Platform API key (`sk-…`) is **not** a ChatGPT web session token.

### Generate the local TOML from your browser

Paste [`scripts/probe-chatgpt.js`](scripts/probe-chatgpt.js) into DevTools on your signed-in `https://chatgpt.com` tab. Enter the optional workspace account ID when prompted. After the checks, accept **Download mise.local.toml**, then enter the export output directory. The download contains a ready-to-use `[env]` table with `CHATGPT_ACCESS_TOKEN`, `CHATGPT_WORKSPACE_ID`, `CHATGPT_USER_AGENT` (copied from `navigator.userAgent`), `CHATGPT_OUTPUT_DIR`, `CHATGPT_BUILD_INDEX`, `CHATGPT_AUXILIARY`, and `CHATGPT_ATTACHMENTS`. Comments explain standalone index rebuilding, automatic project discovery, optional project overrides, and manually copied single/split-cookie alternatives. Decline that prompt for a diagnostic-only run. The console report stays redacted either way; the downloaded file contains the secret token.

The browser saves the file through its normal download mechanism; it cannot automatically update this repository or set file permissions. **In this existing checkout, merge those seven keys under the existing `[env]` in `mise.local.toml` (keep intentional `CHATGPT_BUILD_INDEX`, `CHATGPT_AUXILIARY`, or `CHATGPT_ATTACHMENTS` overrides if already customized)**, preserving your other settings. Do not paste a second `[env]` table or duplicate keys. On a fresh checkout without a local file, use the downloaded file as `mise.local.toml` instead of copying the example. Then run:

```sh
chmod 600 mise.local.toml
mise trust mise.local.toml
mise run probe
```

Delete any redundant download after saving the token locally. Do not share or commit the credential file. Generating it does not prove the token will work outside the browser.

For a managed workspace, copy the `ChatGPT-Account-ID` header from a backend-api request in your browser into `CHATGPT_WORKSPACE_ID`. Use a separate output directory per workspace. A workspace ID is not a project ID.

This working checkout's previously embedded cookies and explicit project list were migrated to the private local file. They are not in the committed example. Both bearer mode and split-cookie-only mode have since passed live probes with the corrected User-Agent; keep credentials current.

## Choose the output directory

Set this under the **existing** `[env]` in private `mise.local.toml`:

```toml
CHATGPT_OUTPUT_DIR = "~/Documents/ChatGPT/personal"
CHATGPT_BUILD_INDEX = "true"
```

Absolute paths, `~`, and relative paths are supported. With mise tasks, relative paths are relative to this repository. `CHATGPT_OUTPUT_DIR` is the destination for export/import and the default source directory for `mise run index`. `--output /another/directory` overrides it for one command. Use separate directories for separate workspaces. The browser credential-file download now asks for this directory too.

## Commands

```sh
# Read-only authentication and response-shape check; no conversation bodies logged.
mise run probe

# Default snapshot: conversations, attachments/artifacts, available memories/settings, and local HTML.
mise run export

# Markdown conversations; auxiliary data still uses JSON.
mise run export -- --format markdown

# Explicitly export conversations only (plus local HTML).
mise run export -- --no-auxiliary --no-attachments

# Backfill files/artifacts in an older JSON export without downloading conversations again.
mise run attachments -- /path/to/previous/export

# Ignore the incremental index and refetch everything.
mise run export -- --refresh

# Offline: import conversations.json or an official export ZIP.
mise run import -- /path/to/export.zip --format markdown --output chatgpt_export_offline

# Build a local webpage from the configured directory, without exporting again.
mise run index

# Or index any older export directory independently (no credentials or network).
mise run index -- /path/to/previous/export

# Help / direct entry point under the mise environment.
mise exec -- uv run --locked -- python chatgpt_exporter.py --help

# Offline quality gates: tests, Ruff, formatting, mypy.
mise run check
```

To request a supported ChatGPT export, open **Settings → Data controls → Export data**. [Availability varies by workspace](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data): self-service export is not available for Business, Enterprise, or Healthcare workspaces. The importer does not request an export; it reads a ZIP or JSON you already downloaded.

### If the HTTP probe gets 403

First match `CHATGPT_USER_AGENT` to your signed-in browser; the helper copies it automatically. A controlled comparison reproduced **403 → 200 → 403** by changing only the User-Agent between the old generic `Mozilla/5.0` and a desktop Chrome string. The default has been corrected. This resolved the observed session and backend failures without a separate browser exporter.

If a response still includes `cf-mitigated: challenge`, the CLI identifies it explicitly. That indicator alone does not establish invalid credentials or prove that a browser transport is required. Other challenge causes and account restrictions remain possible; there is no universal User-Agent fix or challenge solver. See the [diagnosis steps](docs/authentication.md#if-the-cli-detects-a-cloudflare-browser-challenge).

For a check from your real signed-in browser, paste [`scripts/probe-chatgpt.js`](scripts/probe-chatgpt.js) into DevTools on `https://chatgpt.com`. It makes read-only requests using the browser session and returns status codes, field names, types, and counts, not token values or conversation bodies. It prompts for the optional workspace account ID and, afterward, offers a private credential-file download as described above. Inspect the small report before sharing it; never share the downloaded TOML. This is a diagnostic, not a conversation exporter.

## Configuration

All of these can go under `[env]` in `mise.local.toml`:

| Variable | Default | Purpose |
|---|---|---|
| `CHATGPT_ACCESS_TOKEN` | empty | Direct ChatGPT web bearer token |
| `CHATGPT_SESSION_TOKEN` | empty | Single session cookie |
| `CHATGPT_SESSION_TOKEN_0`, `_1` | empty | Split cookie parts (or single cookie in `_0`) |
| `CHATGPT_WORKSPACE_ID` | empty | Optional `ChatGPT-Account-ID` |
| `CHATGPT_USER_AGENT` | Desktop Chrome string; see example | HTTP User-Agent for session and backend requests; helper copies `navigator.userAgent` |
| `CHATGPT_PROJECTS` | `[]` | JSON array of `[project_id_or_url, folder_name]` overrides/additions |
| `CHATGPT_AUTO_PROJECTS` | `true` | Discover personal/shared projects via the sidebar endpoint |
| `CHATGPT_INCLUDE_ARCHIVED` | `false` | Also query the regular archived-conversation listing |
| `CHATGPT_OUTPUT_DIR` | `chatgpt_export` | Export/import destination and default offline-index directory; supports `~` |
| `CHATGPT_BUILD_INDEX` | `true` | Automatically build HTML after export/import; `--no-index` also disables it |
| `CHATGPT_PROGRESS` | `true` | Rich terminal progress or timestamped redirected logs on stderr; `--no-progress` disables it |
| `CHATGPT_AUXILIARY` | `true` | Include available memories/instructions/settings in web exports; `--no-auxiliary` disables, `--auxiliary` forces inclusion |
| `CHATGPT_ATTACHMENTS` | `true` | Download referenced files/images and reconstruct selected-branch Canvas; `--no-attachments` disables |
| `CHATGPT_MAX_ATTACHMENT_BYTES` | `536870912` | Per-asset limit (512 MiB); configurable from 1 byte through 2 GiB |
| `CHATGPT_EXPORT_FORMAT` | `json` | `json` or `markdown` |
| `CHATGPT_PAGE_SIZE` | `100` | Regular listing page size, 1–100 |
| `CHATGPT_REQUEST_DELAY` | `1` | Minimum seconds between requests |
| `CHATGPT_TIMEOUT` | `30` | Connect/read timeout in seconds; not a whole-run deadline |
| `CHATGPT_MAX_RETRIES` | `2` | Per-request retries (at most 3 attempts by default); 0–10 |
| `CHATGPT_MAX_RETRY_WAIT` | `10` | Maximum single autonomous retry sleep; a longer server cooldown defers the run |
| `CHATGPT_MAX_TOTAL_RETRY_WAIT` | `30` | Shared retry-sleep allowance across the command, not reset by each conversation/file |

Example explicit projects:

```toml
[env]
CHATGPT_PROJECTS = '[["g-p-0123456789abcdef0123456789abcdef", "Research"]]'
```

Disabling automatic discovery exports only the regular listing and explicitly configured projects. It is an explicit scope reduction, not a fallback that silently claims all projects were exported.

## Attachments, images, and artifacts

Attachment handling is enabled by default. It recognizes uploaded-file metadata, `sediment://` / `file-service://` pointers, sandbox-generated downloads, and project files returned by project discovery. Canvas create/update operations are materialized locally; ambiguous or unapplied patches are marked partial and retain their operation history. Raw conversation JSON is not rewritten to remove references.

Saved files, checksums, and per-reference outcomes live under `.chatgpt-assets/`. Re-running a JSON export also backfills assets for unchanged conversations; it does not skip attachment work just because the chat file exists. Use `mise run attachments -- /path/to/export` for existing raw JSON, or `--no-attachments` for a conversations-only run. Offline import restores uniquely matched bundled ZIP/local assets and Canvas without contacting the API; missing remote files are recorded rather than silently fetched.

The local viewer adds raster previews, an attachment gallery, and inert file-view/download pages. Archived HTML/SVG/code is never navigated to directly by the archive UI. Browser wrappers are capped at 16 MiB; larger files remain saved locally and can be accessed from the export directory.

**Library authentication:** some `/api/library/files/...` downloads require session cookies even when the conversation API accepts a bearer token. Keep a single cookie or complete `.0`/`.1` pair configured alongside the bearer token when needed. These cookies are sent only to approved ChatGPT content paths, never redirected storage hosts. The helper cannot read HttpOnly cookies; see [authentication](docs/authentication.md) and [attachment details](docs/attachments.md).

Expired/deleted files, unsupported references, or incomplete Canvas reconstruction produce explicit manifest entries and a nonzero partial-export result. This is not a promise that every historical asset remains recoverable.

## Keep continued conversations current

Run the normal export again using the same output directory:

```sh
mise run export
```

A conversation is identified by its ID, not just its title. If its API `update_time` changes, the exporter fetches the full detail again and atomically replaces the current output file. It skips only when the timestamp, selected format/path, and local checksum still match. Missing timestamps or missing/damaged files cause another fetch. New replies and the selected `current_node` are included in the refreshed payload.

Newly fetched entries in private `.export-index.json` record the listing timestamp, exact detail timestamp when returned, last detail-retrieval time (`fetched_at`, UTC), selected node, and checksum. Older unchanged checkpoints gain these audit fields on their next fetch. Skipping an unchanged file does not pretend it was downloaded again. Legacy files without a matching checkpoint are not trusted merely because a filename exists.

To bypass the timestamp/cache decision:

```sh
mise run export -- --refresh
```

This also refreshes enabled attachments, so normal incremental export is preferable for routine updates. `mise run index` only rebuilds the webpage from local files; it does not fetch new messages. Mixed JSON/Markdown views prefer the newer saved version; exact Markdown timestamps/IDs from a checkpoint are used only after its file checksum matches.

There is no atomic whole-account snapshot while chats are changing. A conversation continued after its detail was fetched needs another run. Export after a conversation finishes, and rerun if you used it during the export. Forced refresh removes reliance on unchanged listing timestamps, but still captures each conversation only as of its own request.

## Bounded retries and rate limiting

The default retries use short local delays (1s, 2s) when there is no valid server `Retry-After`. A valid server cooldown is honored **or the run is deferred**, never shortened and retried early. More than 10s for one pause, more than 30s of cumulative retry sleep, or exhausted 429/network retries stops the command with **exit 75** and retains completed checkpoints. This avoids minute-long pauses restarting for each of hundreds of conversations.

Responses are closed before waiting. Actual transport failures clear connection pools while retaining session cookies; HTTP 429 alone is not treated as proof of a broken connection. Logs include the operation, logical request number, retry attempt, wait source, cumulative budget, and recovery so forward progress is visible.

See [retry policy and resume instructions](docs/retries.md). Increase steady pacing or explicitly raise both wait budgets for an unattended slow run if needed; these controls cannot remove a provider quota. Existing numeric overrides in `mise.local.toml` still win over changed defaults. Stop any already-running old process with Ctrl+C; a code update cannot change its in-memory retry policy.

## Progress and run status

Progress is enabled by default for `export`, `import`, `index`, and `probe`. Terminals show live stage bars/spinners, counts, elapsed time, and ETA when a total is known; wider terminals also show percentages and item rates. Redirected stderr receives timestamped stage updates instead of terminal control codes. Progress goes to **stderr**, so the probe's stdout remains valid JSON.

Reporting covers authentication, regular/archived pages, project-discovery pages, per-project conversation listings, deduplicated totals, saved/skipped/failed/pending conversations, bytes written, HTTP attempts/status/response sizes, pacing/backoff, each auxiliary endpoint, directory scans, chat-page rendering, and catalog writing. Unknown cursor/scan totals stay indeterminate rather than showing a made-up percentage. ETAs are estimates.

```sh
# Default conversations + auxiliary snapshots + local HTML; all stages report progress.
mise run export

# Preserve the JSON result while retaining human-readable progress:
mise run probe > probe-result.json

# Suppress progress only; final results and errors remain:
mise run export -- --no-progress
```

You can also set `CHATGPT_PROGRESS = "false"` under the existing `[env]` in private `mise.local.toml`. Chat titles, project names/IDs, credentials, and response bodies are not included in progress. Auxiliary progress counts **endpoint snapshots**, not individual memories or proof of a complete account backup.

On Ctrl+C, the terminal is restored, saved conversation checkpoints remain, and the CLI exits 130. Auxiliary availability is checkpointed throughout the run with explicit saved/unavailable/failed/interrupted/not-attempted outcomes. Optional unavailable endpoints may coexist with exit status 0; inspect `auxiliary-status.json` even when the command finishes. See [progress details](docs/progress.md).

## Output and incremental behavior

```text
chatgpt_export/
├── index.html                            # open directly in a browser
├── .chatgpt-index/chats/…html              # generated readable chat pages
├── .chatgpt-index/assets.html             # attachment gallery, when references exist
├── .chatgpt-assets/manifest.json           # reference/status/hash inventory
├── .chatgpt-assets/files/…                # downloaded binaries
├── .chatgpt-assets/canvas/…               # Canvas text and operation histories
├── .export-index.json
├── _Uncategorized/
│   └── Title--<conversation-id-hash>.json
├── Research--<project-id-hash>/
│   └── Title--<conversation-id-hash>.json
├── auxiliary-status.json                 # default web export; opt out with --no-auxiliary
├── memories/memories.json                # if available
└── settings/…                            # if available
```

- ID-derived suffixes distinguish identical titles and same-named projects regardless of listing order.
- Skip only when ID, format, update timestamp, output path, and file checksum match. Missing timestamps are refetched. `--refresh` bypasses skipping.
- Writes are atomic and files are owner-readable/writable only on POSIX. New output folders use mode 0700. Existing directory permissions are not changed.
- Raw JSON retains all branches and structured content. Markdown follows `current_node` ancestry; older archives without it fall back to the first-child branch. Attachment downloads are separate from Markdown rendering; saved assets are linked by the local viewer. Remote citation/web images without ChatGPT asset references are not fetched.
- Renamed/moved conversations produce a new path; old files are retained, not deleted. Existing exports using the original filename scheme are also left untouched and are not trusted as an incremental index.
- Broken pagination/listing fails rather than reporting a partial listing as success. Individual unavailable conversations produce a nonzero exit; required-request 401/403 stops the export. Auxiliary snapshots are enabled by default: inaccessible endpoints (plain 403/404/405) are recorded as unavailable and do not prevent the remaining checks. A 401 or explicit browser challenge still stops the run. Earlier valid snapshots are not replaced by error payloads.
- Do not run two exports into the same output directory concurrently. An export is not a point-in-time snapshot if conversations change during pagination. Deleted/temporary chats and inaccessible workspace data cannot be recovered.

The default export folders, local credentials, HAR files, virtual environment, and index caches are ignored by Git. If you choose a custom output path, place it under an ignored directory or add an ignore rule before committing anything.

## Browse existing exports locally

```sh
mise run index -- /path/to/old/export
open /path/to/old/export/index.html   # macOS; otherwise open the file in your browser
```

This only scans files already on disk. It does not log in, call an API, run an export, or modify the source JSON/Markdown files. Indexing works even if your saved API credentials/configuration are invalid.

The page provides project/folder counts, title/folder/filename search, project filters, update/title/project sorting, readable chat pages, and links to the original JSON/Markdown files. It works directly over `file://`: no local server, npm, CDN, remote fonts, or external requests. See [local index details](docs/local-index.md) for supported layouts, deduplication, templates, and limits.

New exports/imports build the HTML automatically. Disable that with `CHATGPT_BUILD_INDEX = "false"` or `--no-index`, then rebuild explicitly whenever needed. Moving the whole archive preserves relative links; keep the hidden `.chatgpt-index` and `.chatgpt-assets` directories with `index.html`. Generated pages contain private conversation text, not just metadata: do not commit, publish, or share them inadvertently.

## Development

`export_config.py` validates environment configuration; `chatgpt_client.py` owns read-only transport, authentication, retries, and pagination; `chatgpt_exporter.py` owns offline import and the CLI. `conversation_format.py` shares pure rendering between exports and the viewer; `export_io.py` owns atomic private output; `local_index.py` scans archives and builds pages using `templates/index.html` and `templates/conversation.html`. `main.py` delegates to that CLI. `progress_report.py` owns shared progress rendering and stream separation. Attachment discovery, transfers, Canvas replay, and safe file views are isolated in `attachment_refs.py`, `attachment_transport.py`, `attachments.py`, `canvas_artifacts.py`, and `attachment_view.py`. Tests disable real network access and clear inherited ChatGPT credentials. CI runs only offline gates and needs no ChatGPT secrets.

## License

[MIT](LICENSE). Original copyright and upstream history retained.
