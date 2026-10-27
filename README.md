# ChatGPT Conversation Exporter

Snapshot ChatGPT conversations, attachments, and available account settings, then browse them offline. Export conversations as JSON or Markdown, grouped by project. Repeated exports reuse unchanged conversation files and checksum-verified attachments.

You can also import an official ChatGPT data-export ZIP **without credentials or ChatGPT network access**.

This is an independent derivative of [richlow/chatGPT_extractor](https://github.com/richlow/chatGPT_extractor), preserving its Git history and [MIT license](LICENSE). Web mode uses undocumented ChatGPT endpoints, not the OpenAI Platform API. Endpoint and plan compatibility can change; this is not a guarantee of a complete account backup.

## Install

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). The recommended [mise](https://mise.jdx.dev/getting-started.html) setup installs pinned Python, uv, and the development secret scanner:

```sh
git clone https://github.com/josephjohncox/chatGPT_extractor.git
cd chatGPT_extractor
mise trust mise.toml
mise install --locked
mise run deps
```

Mise selects tools and loads local environment settings; uv owns dependencies and `.venv`. No manual virtualenv activation is needed.

**Already use uv without mise?** Run `uv sync --locked`, then replace `mise run <command> -- <arguments>` below with `uv run --locked -- python chatgpt_exporter.py <command> <arguments>`. Supply web credentials through environment variables; direct Python/uv use does not load `mise.local.toml`.

## Try it offline first

No ChatGPT login is needed for these commands:

```sh
# Synthetic example, not a real conversation:
mise run import -- examples/conversations.json --output exports/demo --no-attachments

# Or import a ZIP/JSON downloaded from ChatGPT:
mise run import -- /path/to/export.zip --output exports/imported

# Browse an existing export without downloading anything:
mise run index -- /path/to/previous/export
```

Open the reported `index.html` in a browser. It works over `file://`; no server, npm, CDN, or remote fonts are required. Existing JSON/Markdown files are preserved. Keep `.chatgpt-index/` and `.chatgpt-assets/` beside `index.html` when moving the archive.

To obtain an official export, use ChatGPT **Settings → Data controls → Export data**, where available. [Availability varies by plan/workspace](https://help.openai.com/en/articles/7260999-how-do-i-export-my-chatgpt-history-and-data); the importer reads a downloaded archive and does not request it for you.

## Upload an archive to Open WebUI

Preview all supported data without making network requests, or inspect the full CLI help:

```sh
uv run python openwebui_import.py chatgpt_export_oct_2026 --all
uv run python openwebui_import.py --help
```

`--all` selects chats, project folders, memories, your personal account system prompt, available artifact files, Canvas histories, and editable text/Markdown Canvas Notes. Add `--upload` to write them to Open WebUI. This updates your personal system prompt from the exported instructions, not global server or shared model prompts. Use `--all --instructions prompt` to save a reusable prompt instead. Omit `--all` for chats only. Missing memory/instruction files stop validation; missing/partial artifacts are reported. General settings are not included.

Add `--upload` after setting `OPENWEBUI_URL` and `OPENWEBUI_TOKEN` to import chats with their original dates and visible branches. Start with `--upload --limit 1`. A local journal lets later runs skip completed uploads. Add `--project-folders` to create folders for ChatGPT projects and assign chats during import. Unique exact-name folders in your account are reused without changing their settings; ambiguous or conflicting matches stop rather than create duplicates. Use `--include-artifacts` for downloadable files and histories, or `--canvas-notes` for those plus editable Notes; both are included in `--all`. Files are checksum-verified and uploaded without extraction/embedding. **`--limit` limits chats only, not files or extras.** Moving previously imported chats requires `--move-existing-project-chats`, which permits Open WebUI to change update dates and clear pinned/archive state.

Use `--include-memories` to import saved memories and `--instructions prompt` to create a reusable custom-instructions prompt. Use `--instructions system` to update your personal account system prompt, replacing a different existing personal prompt while retaining other settings. This is the default with `--all`. Add `--only-extras` to skip chats. Memory imports invoke the server's embedding service and receive new timestamps.

To recreate earlier imports with original dates plus all selected features, use `--all --replace-imported-chats --upload` with the **same journal**. This deletes only journaled chats whose owner and source IDs are verified, then re-imports them. It discards server-only edits/shares and changes server chat IDs; back up Open WebUI first. Files, Notes, memories, prompts, and unrelated chats are not cleared. `--all` alone never enables deletion. Alternatively, `--update-existing-artifacts` adds links without deleting chats but changes update dates.

Recognised static layout tags in assistant messages become readable Markdown by default, including safe literal `http`/`https` `<Link>` components. Mermaid and other existing code blocks are preserved. Use `--layout-rendering html` to also generate static HTML artifact previews, or `--layout-rendering source` to retain raw markup. Unsupported or dynamic widgets remain explicitly labelled source, and original message data is always retained.

By default, historical tool calls/results, commentary, and reasoning nodes fold into a collapsed **Imported activity** section above each branch's final answer—rather than appearing as raw JSON bubbles. They remain inert records and are never re-executed. Tool calls never become live Open WebUI tool actions. Explicit Mermaid source in historical records and Confluence Mermaid macro bodies gets a separate collapsed diagram preview; arbitrary HTML and other macro types remain source-only. Use `--activity-rendering nodes` for one bubble per source node. Exported ChatGPT reasoning summaries/recaps and explicitly marked thinking text become collapsible reasoning blocks. Source branches and raw source JSON remain available; no missing internal reasoning or provider session state is invented.

Open WebUI can search imported chat titles and messages through sidebar Search. Tool-capable models can use `search_chats` and `view_chat` with Native Function Calling and Builtin Tools enabled.

The current migration journal records 562 completed replacements. To apply newly added rendering to those same imports while retaining source dates, first back up server-only changes and rerun `--all --replace-imported-chats --upload` with the same journal. See [Open WebUI import](docs/openwebui-import.md) for rendering modes, search setup, folder migration, credentials, timestamp handling, and recovery after an interrupted upload.

## Set up a live web snapshot

1. Copy the example **only if you do not already have a local config**:

   ```sh
   cp -n mise.local.toml.example mise.local.toml
   chmod 600 mise.local.toml
   mise trust mise.local.toml
   ```

   On Windows, copy without replacing an existing file and restrict its access through Windows permissions instead of `chmod`.

2. Follow [Get ChatGPT session tokens](docs/authentication.md). Choose a bearer token or single/split session cookies. An OpenAI Platform API key is not a ChatGPT web token.

   Alternatively, read and paste [`scripts/probe-chatgpt.js`](scripts/probe-chatgpt.js) into DevTools on your signed-in `https://chatgpt.com` tab. It offers a private `mise.local.toml` download with browser token, User-Agent, workspace, and output settings. It cannot read HttpOnly cookies. Merge its keys under the existing `[env]` rather than replacing other settings or adding a second table. Never share the downloaded file.

3. Choose an output directory under the existing `[env]`:

   ```toml
   CHATGPT_OUTPUT_DIR = "exports/personal"
   CHATGPT_BUILD_INDEX = "true"
   ```

   Absolute paths and `~` are supported. Mise tasks resolve relative paths from the checkout. Use a separate output directory for each account/workspace. The default `chatgpt_export` and `exports/` are Git-ignored; custom paths outside those locations need their own ignore rule.

4. Check authentication before a bulk run:

   ```sh
   mise run probe
   mise run export
   ```

Credentials belong only in ignored local config or environment variables, never source, command-line arguments, issues, or logs. Bearer authentication takes precedence and does not automatically fall back if expired. Some library downloads need session cookies alongside a bearer token. See [authentication and troubleshooting](docs/authentication.md).

## Common commands

| Task | Command |
|---|---|
| Check authentication and metadata shapes | `mise run probe` |
| Incremental snapshot + offline HTML | `mise run export` |
| Export Markdown | `mise run export -- --format markdown` |
| Conversations only + HTML | `mise run export -- --no-attachments --no-auxiliary` |
| Backfill assets from saved JSON | `mise run attachments -- /path/to/export` |
| Rebuild HTML locally | `mise run index -- /path/to/export` |
| Force enabled conversation/asset work | `mise run export -- --refresh` |
| ZIP the configured directory; keep it | `mise run zip` |
| Restore its ZIP into a new sibling; keep both | `mise run unzip` |
| Show all CLI options | `mise exec -- uv run --locked -- python chatgpt_exporter.py --help` |

## What gets saved

The default web export attempts:

- Conversations from regular/project listings; archived conversations are opt-in.
- Referenced uploaded/generated files, images, audio, project uploads, and selected-branch Canvas reconstruction.
- Available memories, custom instructions, settings, and account metadata.
- A searchable local HTML index, readable chat pages, and an attachment gallery.

```text
chatgpt_export/
├── index.html
├── _Uncategorized/…json
├── Project--<id-hash>/…json
├── .export-index.json
├── .chatgpt-index/          # readable pages and safe asset viewers
├── .chatgpt-assets/         # manifest, downloaded files, Canvas histories
├── auxiliary-status.json
├── memories/
└── settings/
```

Outputs contain private account data, even though this repository's code can be public. ZIP backups are **not encrypted**. Do not publish generated HTML, raw exports, backups, tokens, or HAR files. Archived HTML/SVG/code is handled through inert viewers rather than executed by the archive UI. See [security guidance](SECURITY.md).

## Caching, progress, and partial results

Run `mise run export` into the same directory for routine updates. An unchanged ID/listing timestamp/format/path with a matching local checksum is reused. Continued conversations are fetched again when their listing timestamp changes. Missing/damaged files and `--refresh` trigger fresh retrieval. Renames/moves may also require a new fetch; older copies are retained.

`Saved: 0; unchanged: 556; failed: 0` means conversation bodies were reused—not downloaded 556 times. Metadata listing pages are still requested to discover changes. Attachment processing can read cached conversation JSON and verify saved files locally. Unavailable/failed assets are attempted again on later runs; negative-result cooldowns are not currently implemented.

Attachment progress stays in one panel with aggregate counts and a reusable current activity. Sizes/rates use readable units such as `9.0 MiB/20.0 MiB` and `512.0 KiB/s`. Progress uses stderr; results use stdout. Use `--no-progress` or `CHATGPT_PROGRESS = "false"` to suppress progress.

| Exit status | Meaning and next action |
|---|---|
| `0` | Command completed; inspect auxiliary availability for optional gaps. |
| `1` | Partial snapshot or another error. Read the summary/manifest; existing files are retained. |
| `2` | Invalid command-line usage. |
| `75` | Temporary retry/rate-limit stop. Respect the reported cooldown, then resume. |
| `130` | Ctrl+C interruption. Resume using the same directory. |

**`[export] ERROR task failed` does not mean your saved conversations were lost.** If conversation failures are zero but attachments are partial, the nonzero status reports missing assets. A progress bar at 100% means processing finished, not that every historical file was recoverable. Inspect `.chatgpt-assets/manifest.json` and `auxiliary-status.json`. Expired/deleted files, access restrictions, and ambiguous Canvas patches remain explicit gaps rather than false success.

Retry defaults are two retries per request, a 10-second single-wait cap, and a 30-second shared retry-sleep allowance. Valid server `Retry-After` values are respected or cause a deferred stop, never shortened. See [retry configuration](docs/retries.md).

If a probe gets an explicit Cloudflare challenge, match `CHATGPT_USER_AGENT` to your signed-in browser and follow the [diagnosis guide](docs/authentication.md#if-the-cli-detects-a-cloudflare-browser-challenge). A User-Agent is not a full browser transport or universal challenge solver. Tokens expire, account access varies, and endpoint behavior can change.

## ZIP backup and restore

```sh
mise run zip                                      # chatgpt_export -> chatgpt_export.zip
mise run unzip                                    # -> chatgpt_export_restored
mise run zip -- /path/to/export --output /path/to/backup.zip
mise run zip -- --overwrite                       # explicitly replace an older ZIP
mise run unzip -- /path/to/backup.zip --output /new/restore-directory
```

The source directory and ZIP are retained. Hidden assets/checkpoints and empty directories are included. Restore refuses an existing destination, unsafe paths, symlinks, and conflicting entries. These commands are offline and do not validate credentials. Stop any writer before backing up: this is not an atomic snapshot of a running export. See [backup limits and restore verification](docs/backups.md).

## Documentation

- [Authentication and cookie setup](docs/authentication.md)
- [Configuration reference and defaults](docs/configuration.md) · [`mise.local.toml.example`](mise.local.toml.example)
- [Snapshot scope](docs/snapshots.md) · [Attachments and Canvas](docs/attachments.md)
- [Offline browser/index](docs/local-index.md) · [Progress](docs/progress.md)
- [Retries and resume](docs/retries.md) · [ZIP backup/restore](docs/backups.md)
- [API compatibility and scoped verification](docs/api-compatibility.md)
- [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

## Development

```sh
mise run deps
mise run check     # offline tests, Ruff, formatting, mypy
mise run secrets   # redacted Gitleaks scan of all fetched history
```

Tests block real network calls and clear inherited ChatGPT credentials. CI uses no account secrets, scans full Git history, and runs offline gates. Use synthetic fixtures when reporting bugs; do not attach real export files. Original copyright and upstream history are retained under the [MIT license](LICENSE).
