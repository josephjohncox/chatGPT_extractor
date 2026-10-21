# ChatGPT Conversation Exporter

Private derivative of [richlow/chatGPT_extractor](https://github.com/richlow/chatGPT_extractor), retaining the upstream Git history and MIT license. GitHub does not support making a public repository's fork private, so this is an independent private repository, not a GitHub fork-network member.

Export ChatGPT web conversations to raw JSON (default) or Markdown, grouped by project. Or convert a downloaded ChatGPT data export **offline**, without credentials or network access.

**Live compatibility is not yet verified for this account.** The session endpoint returned HTTP 403, and an isolated Chrome visit reached Cloudflare's challenge page. The web mode uses undocumented endpoints; no claim of universal plan support is made. See [API and UI investigation](docs/api-compatibility.md).

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

Choose one authentication method in that file:

- **Bearer token:** in a signed-in browser, open `https://chatgpt.com/api/auth/session` and place its `accessToken` value in `CHATGPT_ACCESS_TOKEN`.
- **Single session cookie:** copy `__Secure-next-auth.session-token` from browser DevTools → Application → Cookies → `https://chatgpt.com` into `CHATGPT_SESSION_TOKEN`.
- **Split cookies:** copy `.0` and `.1` values into `CHATGPT_SESSION_TOKEN_0` and `CHATGPT_SESSION_TOKEN_1`. A single cookie in `_0` is also accepted for compatibility.

Bearer authentication takes precedence; a single cookie takes precedence over split cookies. Tokens expire. There is no automatic fallback from an expired bearer token: rerun the helper to refresh it, or clear `CHATGPT_ACCESS_TOKEN` to use cookie-based session exchange. An OpenAI Platform API key (`sk-…`) is **not** a ChatGPT web session token.

### Generate the local TOML from your browser

Paste [`scripts/probe-chatgpt.js`](scripts/probe-chatgpt.js) into DevTools on your signed-in `https://chatgpt.com` tab. Enter the optional workspace account ID when prompted. After the checks, accept **Download mise.local.toml**, then enter the export output directory. The download contains a ready-to-use `[env]` table with `CHATGPT_ACCESS_TOKEN`, `CHATGPT_WORKSPACE_ID`, `CHATGPT_OUTPUT_DIR`, and `CHATGPT_BUILD_INDEX`. Comments explain standalone index rebuilding, automatic project discovery, optional project overrides, and manually copied single/split-cookie alternatives. Decline that prompt for a diagnostic-only run. The console report stays redacted either way; the downloaded file contains the secret token.

The browser saves the file through its normal download mechanism; it cannot automatically update this repository or set file permissions. **In this existing checkout, merge those four keys under the existing `[env]` in `mise.local.toml` (keep your chosen `CHATGPT_BUILD_INDEX` value if already customized)**, preserving your other settings. Do not paste a second `[env]` table or duplicate keys. On a fresh checkout without a local file, use the downloaded file as `mise.local.toml` instead of copying the example. Then run:

```sh
chmod 600 mise.local.toml
mise trust mise.local.toml
mise run probe
```

Delete any redundant download after saving the token locally. Do not share or commit the credential file. Generating it does not prove the token will work outside the browser.

For a managed workspace, copy the `ChatGPT-Account-ID` header from a backend-api request in your browser into `CHATGPT_WORKSPACE_ID`. Use a separate output directory per workspace. A workspace ID is not a project ID.

This working checkout's previously embedded cookies and explicit project list were migrated to the private local file. They are not in the committed example. The existing cookie values have not been proven usable.

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

# Discover projects and export conversations as JSON.
mise run export

# Markdown instead; optional memories/settings snapshots.
mise run export -- --format markdown --auxiliary

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

403 can mean invalid credentials, a workspace/access restriction, or browser/Cloudflare gating. Refreshing a token is not guaranteed to fix it. The tool does not try to solve or bypass challenges.

For a check from your real signed-in browser, paste [`scripts/probe-chatgpt.js`](scripts/probe-chatgpt.js) into DevTools on `https://chatgpt.com`. It makes read-only requests using the browser session and returns status codes, field names, types, and counts, not token values or conversation bodies. It prompts for the optional workspace account ID and, afterward, offers a private credential-file download as described above. Inspect the small report before sharing it; never share the downloaded TOML. This is a diagnostic, not a conversation exporter.

## Configuration

All of these can go under `[env]` in `mise.local.toml`:

| Variable | Default | Purpose |
|---|---|---|
| `CHATGPT_ACCESS_TOKEN` | empty | Direct ChatGPT web bearer token |
| `CHATGPT_SESSION_TOKEN` | empty | Single session cookie |
| `CHATGPT_SESSION_TOKEN_0`, `_1` | empty | Split cookie parts (or single cookie in `_0`) |
| `CHATGPT_WORKSPACE_ID` | empty | Optional `ChatGPT-Account-ID` |
| `CHATGPT_PROJECTS` | `[]` | JSON array of `[project_id_or_url, folder_name]` overrides/additions |
| `CHATGPT_AUTO_PROJECTS` | `true` | Discover personal/shared projects via the sidebar endpoint |
| `CHATGPT_INCLUDE_ARCHIVED` | `false` | Also query the regular archived-conversation listing |
| `CHATGPT_OUTPUT_DIR` | `chatgpt_export` | Export/import destination and default offline-index directory; supports `~` |
| `CHATGPT_BUILD_INDEX` | `true` | Automatically build HTML after export/import; `--no-index` also disables it |
| `CHATGPT_EXPORT_FORMAT` | `json` | `json` or `markdown` |
| `CHATGPT_PAGE_SIZE` | `100` | Regular listing page size, 1–100 |
| `CHATGPT_REQUEST_DELAY` | `1` | Minimum seconds between requests |
| `CHATGPT_TIMEOUT` | `30` | Per-request timeout in seconds |
| `CHATGPT_MAX_RETRIES` | `5` | Retries for network failures, 429, and selected 5xx responses; 0–10 |

Example explicit projects:

```toml
[env]
CHATGPT_PROJECTS = '[["g-p-0123456789abcdef0123456789abcdef", "Research"]]'
```

Disabling automatic discovery exports only the regular listing and explicitly configured projects. It is an explicit scope reduction, not a fallback that silently claims all projects were exported.

## Output and incremental behavior

```text
chatgpt_export/
├── index.html                            # open directly in a browser
├── .chatgpt-index/chats/…html              # generated readable chat pages
├── .export-index.json
├── _Uncategorized/
│   └── Title--<conversation-id-hash>.json
├── Research--<project-id-hash>/
│   └── Title--<conversation-id-hash>.json
├── auxiliary-status.json                 # only with --auxiliary
├── memories/memories.json                # if available
└── settings/…                            # if available
```

- ID-derived suffixes distinguish identical titles and same-named projects regardless of listing order.
- Skip only when ID, format, update timestamp, output path, and file checksum match. Missing timestamps are refetched. `--refresh` bypasses skipping.
- Writes are atomic and files are owner-readable/writable only on POSIX. New output folders use mode 0700. Existing directory permissions are not changed.
- Raw JSON retains all branches and structured content. Markdown follows `current_node` ancestry; older archives without it fall back to the first-child branch. Markdown notes non-text content but does not download attachments, images, audio, or project files.
- Renamed/moved conversations produce a new path; old files are retained, not deleted. Existing exports using the original filename scheme are also left untouched and are not trusted as an incremental index.
- Broken pagination/listing fails rather than reporting a partial listing as success. Individual unavailable conversations produce a nonzero exit; 401/403 stops the export. Optional auxiliary endpoint failures are recorded in `auxiliary-status.json`, without replacing a previous valid snapshot with an error payload.
- Do not run two exports into the same output directory concurrently. An export is not a point-in-time snapshot if conversations change during pagination. Deleted/temporary chats and inaccessible workspace data cannot be recovered.

The default export folders, local credentials, HAR files, virtual environment, and index caches are ignored by Git. If you choose a custom output path, place it under an ignored directory or add an ignore rule before committing anything.

## Browse existing exports locally

```sh
mise run index -- /path/to/old/export
open /path/to/old/export/index.html   # macOS; otherwise open the file in your browser
```

This only scans files already on disk. It does not log in, call an API, run an export, or modify the source JSON/Markdown files. Indexing works even if your saved API credentials/configuration are invalid.

The page provides project/folder counts, title/folder/filename search, project filters, update/title/project sorting, readable chat pages, and links to the original JSON/Markdown files. It works directly over `file://`: no local server, npm, CDN, remote fonts, or external requests. See [local index details](docs/local-index.md) for supported layouts, deduplication, templates, and limits.

New exports/imports build the HTML automatically. Disable that with `CHATGPT_BUILD_INDEX = "false"` or `--no-index`, then rebuild explicitly whenever needed. Moving the whole archive preserves relative links; keep the hidden `.chatgpt-index` directory with `index.html`. Generated pages contain private conversation text, not just metadata: do not commit, publish, or share them inadvertently.

## Development

`export_config.py` validates environment configuration; `chatgpt_client.py` owns read-only transport, authentication, retries, and pagination; `chatgpt_exporter.py` owns offline import and the CLI. `conversation_format.py` shares pure rendering between exports and the viewer; `export_io.py` owns atomic private output; `local_index.py` scans archives and builds pages using `templates/index.html` and `templates/conversation.html`. `main.py` delegates to that CLI. Tests disable real network access and clear inherited ChatGPT credentials. CI runs only offline gates and needs no ChatGPT secrets.

## License

[MIT](LICENSE). Original copyright and upstream history retained.
