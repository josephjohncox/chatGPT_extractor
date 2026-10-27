# Import chats into Open WebUI

`openwebui_import.py` reads an export directory or a ChatGPT `conversations.json` file. It validates the source without contacting a server unless you pass `--upload`.

## Import everything supported

```sh
# Show grouped options, environment variables, examples, and limitations:
uv run python openwebui_import.py --help

# Preview all supported data without network requests:
uv run python openwebui_import.py chatgpt_export_oct_2026 --all

# Upload after setting OPENWEBUI_URL and OPENWEBUI_TOKEN:
uv run python openwebui_import.py chatgpt_export_oct_2026 --all --upload
```

`--all` selects chats, project folders, saved memories, your personal account system prompt, artifact files, Canvas histories, and editable text/Markdown Canvas Notes. It is shorthand for `--project-folders --include-memories --instructions system --canvas-notes`. It requires an export directory; missing memory/instruction files or disabled custom instructions stop validation before uploads. Missing artifact inventories and unavailable assets are reported as gaps; available files are checksum-verified.

`--all --upload` updates your personal account system prompt from the exported custom instructions, replacing any different existing personal prompt. It never changes global server or shared model prompts. Use `--all --instructions prompt` to save a reusable prompt **instead of** updating the personal prompt. `--all` never implies `--upload`, `--move-existing-project-chats`, `--update-existing-artifacts`, or `--replace-imported-chats`. Moving existing chats still requires explicit consent to the timestamp and pinned/archive changes described below. `--all` cannot be combined with `--only-extras`.

`--dry-run` explicitly selects the default validation-only mode and cannot be combined with `--upload`. **`--limit N` limits conversations only, even with `--all`; every selected memory, instruction, artifact file, and Canvas Note is still processed.** Project instructions, automatic folder knowledge configuration, and general settings remain unsupported.

## Preview

```sh
uv run python openwebui_import.py chatgpt_export_oct_2026
```

For this project's exports, the script uses `.export-index.json` to select current JSON files and ignore stale copies. Markdown-only exports cannot restore conversation trees. ZIP files must be extracted first.

## Upload

Set the destination and bearer credential in your environment. Use an Open WebUI API key with access to the auth and chat-import routes, or a session bearer token. The server must allow your account to import chats. Do not put the credential in the command line or commit it to Git.

```sh
export OPENWEBUI_URL='https://your-openwebui.example'
# Set OPENWEBUI_TOKEN through your secret manager or shell environment.

# Import one conversation, then inspect it in Open WebUI:
uv run python openwebui_import.py chatgpt_export_oct_2026 --upload --limit 1

# Import the rest; the completed conversation is skipped:
uv run python openwebui_import.py chatgpt_export_oct_2026 --upload
```

`--url` overrides `OPENWEBUI_URL`. Include any reverse-proxy base path, but not `/api/v1`. Remote instances require HTTPS; localhost HTTP is allowed for local servers and tunnels. TLS certificate verification remains enabled. Redirects are not followed.

Chats belong to the authenticated account. The script checks that account's ID before uploading. It sends one conversation per request to `/api/v1/chats/import`; ordinary chat import does not call a model, delete chats, or overwrite existing conversations. Only the explicit replacement/update options below modify existing chats.

## Projects as folders

```sh
# Preview project assignments:
uv run python openwebui_import.py chatgpt_export_oct_2026 --project-folders

# Import chats directly into project folders, retaining original dates:
uv run python openwebui_import.py chatgpt_export_oct_2026 --project-folders --upload
```

The script recognizes ChatGPT project IDs (`g-p-...`), not custom GPT IDs or workspaces. It recovers display names from this exporter's project directory names. Without a name, it uses the project ID. Different projects with identical names receive an ID suffix so they remain separate. Uncategorized chats remain outside folders. Folders are created only as their chats are processed, so `--limit` also bounds which folders are needed.

New folder IDs are included in the initial chat import. This preserves conversation dates, archive state, and pinned state. Archived chats may remain hidden from ordinary folder listings; use archived search to find them. Project-specific instructions and folder knowledge settings are not transferred. Saved project file references can transfer as downloadable artifacts, not as automatically indexed folder knowledge.

Folder IDs are recorded in the journal's `project_folders` table. Before creating a folder, the importer checks the authenticated user's own folders for an exact name match, including nested folders. One matching folder is reused without modifying its instructions, files, name, parent, or sharing rules. Its existing configuration therefore continues to apply to imported chats. Multiple matches, a conflicting source-project marker, or a folder already mapped to another source project stop the run instead of creating a duplicate. Reruns reuse the journaled ID even if you later rename the folder. Shared folders owned by other users are not adopted. Do not run folder creation concurrently from different journals.

### Chats imported before folder support

Reuse the same journal. Completed chats are not re-imported. If a completed chat is already in the correct folder, nothing changes. Otherwise, the script stops unless you explicitly permit moving it:

```sh
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --project-folders --move-existing-project-chats --upload
```

**Moving an existing chat changes its last-update date to the current time and clears pinned/archive state in current upstream Open WebUI.** Original dates remain in `chat.chatgpt_source`, but the sidebar date changes. The importer cannot preserve the database update date through the move endpoint. This option also permits moving a previously imported chat out of a folder you assigned manually. It does not move uncategorized chats or chats absent from the journal.

After an interrupted move, a rerun checks the current folder before trying again. After an interrupted folder creation, a rerun can recover a unique same-name folder after validating its ID and any source marker. If it cannot, inspect `project_folders` for a null `remote_id`. Find the destination folder and verify `meta.chatgpt_project_id`, then record its ID; delete the pending row only if creation definitely failed. Back up the journal before editing it. Without `--replace-imported-chats`, the script never deletes or re-imports chats to repair folder assignments.

## Artifacts, Canvas, and generated outputs

`--include-artifacts` uploads available manifest-backed files and Canvas snapshots plus their operation-history JSON. `--canvas-notes` also creates private editable Notes for `.md`/`.txt` Canvas snapshots and implies `--include-artifacts`. Both are enabled by `--all`.

The importer validates `.chatgpt-assets/manifest.json`, its ownership marker, paths, byte counts, and SHA-256 hashes before network writes. Symlinks and escaping paths are rejected. Files upload one at a time with `process=false`, so no extraction, embedding, or transcription is requested. Multipart encoding buffers an individual file in memory. The server's per-file size limits still apply.

Files are sent as `application/octet-stream`; chat links explicitly request download. HTML/code artifacts are not executed or turned into live previews. Notes receive a Markdown copy with raw HTML escaped; original bytes remain in the downloadable file. Canvas histories are retained as JSON, not recreated as Open WebUI revision timelines. Partial snapshots are labelled in both Notes and chat links. Notes/files receive server-assigned dates.

Available files are uploaded before chats. Their download links, history links, and Note links are attached to the corresponding visible message or nearest visible ancestor; when no corresponding visible message can be resolved, they are appended to the selected message. Exact sandbox Markdown destinations in that message are rewritten. This is archival linking, not a guarantee that every nested ChatGPT asset pointer is translated or that binary contents become model context. Source JSON remains unchanged. Saved project-scoped files without a conversation are linked to matching project chats.

The `artifact_uploads` journal table tracks each file/history/Note separately. Reruns reuse completed IDs; changed files and uncertain uploads stop instead of duplicating content. For a pending file, inspect its server metadata (`chatgpt_asset_id` inside file metadata data), part, size and checksum before recording its remote ID. For a pending Note, match the source asset metadata and content. Do not delete the journal. Files/Notes deleted manually on the server require manual reconciliation; the script does not automatically recreate them.

Unavailable/failed source assets are reported and skipped, not silently declared recovered. An absent manifest is reported as a gap. Successful exit means the selected available data was handled, not that the ChatGPT backup was complete. Keep the original archive.

For already-imported chats, choose one of:

- `--update-existing-artifacts`: append links to existing server message content. This changes the last-update date. Do not edit the chats concurrently; the API lacks conditional updates.
- `--replace-imported-chats`: recreate verified journaled chats with source dates and all selected features, as described below.

Without either option, a previously imported chat that lacks the selected links stops the run. Already-present identical links are skipped. Artifact files may already have been uploaded at that point and will be reused on retry.

## Replace only this import's chats, preserving source dates

Back up your Open WebUI chats first, then preview:

```sh
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --all --replace-imported-chats

# Performs deletion and re-import only when --upload is also present:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --all --replace-imported-chats --upload
```

This is **not** a bulk “delete all chats” operation. For each selected source conversation, the importer:

1. Looks up the completed server chat ID in the same destination/account journal.
2. Fetches that exact chat and verifies its owner, remote ID, and source conversation ID. All present source-ID markers must agree. Missing or conflicting identity stops deletion.
3. Records replacement intent durably, deletes that one verified chat, and imports the source anew with original creation/update dates, archive/pinned state, and selected folders/artifact links.

If an old journaled chat was already deleted manually, Open WebUI 0.12.4 may return HTTP 401 with “We could not find what you're looking for :/” rather than 404. The importer distinguishes that response from an invalid token. With `--replace-imported-chats`, it checks all ordinary and archived chat-ID pages (including pinned/folder chats) before clearing the stale chat journal entry and restoring it. A listed-but-inaccessible chat or failed inventory check stops recovery. Files, Notes, memories, and prompt journal entries are not cleared. Do not delete the whole journal to fix this condition.

The replacement receives a **new server chat ID**. Server-only edits, continuations, share links, and old bookmarks are not preserved. Unrelated chats and chats imported outside this journal are never deleted. Their IDs may be listed read-only to confirm that a journaled chat is absent. Existing files, folders, Notes, memories and prompts are reused, not cleared. Source conversations absent from this run are untouched.

`--limit` also limits replacements. Do not run concurrent migrations or edit selected chats during replacement. Replacement cannot be combined with the existing-chat move/update options or `--only-extras`. It can be used without `--all` to replace chats with only the features explicitly selected.

Deletion and re-import are not a server-side transaction. The `chat_replacements` table records `prepared`, `deleted`, and `complete` states. An interruption after a recorded deletion can resume without deleting another chat. An ambiguous re-import still requires normal pending-import reconciliation. Rerunning the same payload/options skips completed replacements, so resuming does not repeatedly delete the new chats. Changed replacement options/source during incomplete recovery are rejected. A new desired payload can trigger another explicitly requested replacement.

## Rich layouts and Mermaid diagrams

Assistant messages containing static `<box>`, `<grid>`, or `<row>` layout blocks are converted to readable Markdown by default, including with `--all`. The supported child vocabulary includes `grid-item`, `text`, `title`, `caption`, `badge`, `icon`, `divider`, `spacer`, `table`, `table-row`, `table-cell`, `Link`, `Entity`, and `Cite`. Literal `http`/`https` links become Markdown links and entities become their readable values.

When a ChatGPT `Cite` source ID matches an entry in the same message's exported `search_result_groups`, the importer emits Open WebUI numeric citation tokens and attaches source-backed citation objects containing the original title, URL, and snippet. Open WebUI can then display the inline citation and source modal/link. Unresolved internal IDs remain explicit archival citation text rather than being fabricated as links. Existing source objects retain their numbers; repeated references share a source entry. Simple label/value card grids become Markdown tables. Other supported layouts become paragraphs, rows, and emphasis. Icon names remain textual labels rather than guessed graphics.

```sh
# Default: render recognised layouts inline as Markdown:
uv run python openwebui_import.py chatgpt_export_oct_2026 --all

# Also emit static HTML code blocks for Open WebUI's artifact preview:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --all --layout-rendering html

# Keep layout markup unchanged instead (reasoning conversion still applies):
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --all --layout-rendering source
```

HTML mode keeps the inline Markdown fallback and adds a standalone HTML/CSS artifact block. It approximates cards, grid columns, gaps, padding, typography, and colours; it is not a copy of ChatGPT's component renderer. Only fixed generated HTML/CSS and escaped text are emitted. No JavaScript, arbitrary attribute forwarding, or network resources are added. The HTML contains a restrictive content-security policy. Actual preview availability depends on the Open WebUI client/settings.

Existing fenced code blocks (including Mermaid), inline code, indented code, user messages, and structured code content are not reinterpreted. Valid assistant `mermaid` fences remain available to Open WebUI's native diagram renderer; the importer does not repair invalid Mermaid syntax or execute diagrams server-side. Ordinary prose around converted layouts is retained.

Static SVG elements (`svg`, `rect`, `path`, `g`, and `text`) are converted into fenced Mermaid flowcharts by default, which Open WebUI renders directly in chat and inside collapsed activity sections. Literal SVG labels and simple line geometry produce nodes and directional edges. The original SVG stays in `chatgpt_source`; this is a semantic visual approximation rather than pixel-exact SVG rendering. `--layout-rendering html` retains a generated static SVG in the accompanying HTML artifact for cases where the original vector layout is preferable. Literal Svelte `{#each [...] as x}` loops expand from JSON-like scalar object data, including basic numeric arithmetic such as `x.x+49`; this supports the exported card/arrow diagrams without a Svelte runtime. Dynamic collection expressions, nonliteral values, unsupported expressions, script-bearing elements, event handlers, unsafe URLs, malformed blocks, and over-limit trees remain explicitly labelled source-code blocks. Maps, custom citation widgets, React components, and other unknown components are not silently guessed. Limits are 128 KiB per layout scan, 2,048 nodes, and nesting depth 32. Original message content remains in `chat.chatgpt_source` in every mode.

Rendering choices affect the import digest. Existing chats with old/raw rendering are not silently overwritten. To apply the new rendering while retaining source dates, back up Open WebUI, then rerun with the same journal and `--all --replace-imported-chats --upload` (optionally add `--layout-rendering html`). This replacement discards server-only edits and changes server chat IDs, as described above. Do not change rendering options during an incomplete replacement recovery.

## Historical tool activity and thinking blocks

By default, the importer renders **one visible assistant bubble per completed turn**, rather than a bubble for every historical tool call, tool result, thinking summary, recap, and commentary node. This is `--activity-rendering compact`, and it is enabled with `--all`.

```sh
# Default: compact historical activity into the answer it led to:
uv run python openwebui_import.py chatgpt_export_oct_2026 --all

# Diagnostic/forensic view: keep one bubble per exported source node:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --all --activity-rendering nodes
```

In compact mode, each branch's activity records appear in a collapsed **Imported activity** section immediately above the branch's final answer. Tool calls and tool results are marked historical and are never executable Open WebUI tool calls: the importer does not create `tool_calls`, `output`, approval actions, or live MCP invocations. Tool JSON is displayed as escaped code with a 64 KiB preview limit; the complete original record remains in `chatgpt_source`.

Explicit fenced Mermaid blocks inside historical tool records, plus Confluence `macro-diagram` bodies whose embedded syntax is explicitly `Mermaid`, receive an additional collapsed **Historical Mermaid diagram** preview. This is a visual archival rendering, not a replay of the tool action or macro. Arbitrary HTML, JavaScript, malformed macro metadata, non-Mermaid diagram types, and oversized diagrams stay code/source only. A branch with no final answer gets an explicit incomplete-turn marker. Sibling branches retain their own activity and do not inherit each other's calls/results.

Hidden source nodes remain only in `chatgpt_source`. The collapsed activity metadata records the represented source node IDs, kinds, and timestamps. Artifact links from a folded source node resolve to the answer(s) for that branch, not to a phantom tool bubble. `--activity-rendering nodes` is available for inspection, but it still represents historical calls as inert collapsible records rather than live tool activity.

Reasoning conversion is enabled in both activity modes, including with `--all`. ChatGPT's exported `thoughts` and `reasoning_recap` records are rendered as Open WebUI saved-history blocks:

```html
<details type="reasoning" done="true">
<summary>Imported ChatGPT reasoning summary</summary>

> The reasoning summary text actually present in the export.

</details>
```

This uses Open WebUI's stored `<details>` format rather than relying on its live-stream `<think>` parser to run during import. Thought summaries and their available content are extracted from structured records, not displayed as JSON answers. Recaps retain cancellation labels; a finite nonnegative `finished_duration_sec` becomes the duration attribute when present. Missing durations are not invented.

Assistant text explicitly marked as an analysis channel, thinking preamble, or active reasoning is also folded. Ordinary commentary and final answers are not inferred to be reasoning just because they occur in the same turn. Tool invocations/results remain separately labelled historical activity, never reasoning unless their own exported record is explicitly a reasoning item. Complete inline `<think>`, `<thinking>`, `<reason>`, `<reasoning>`, `<thought>`, and `<|begin_of_thought|>` pairs are converted outside Markdown code. Fenced/inline/indented examples and structured code content remain unchanged; unmatched tags are not repaired. User messages are not transformed.

Separate source message IDs, ancestry, branches, timestamps, and the selected final answer are preserved in `chatgpt_source`. Compact display deliberately folds intermediary nodes into their branch's answer bubble; node mode exposes them individually. Hidden messages remain only in the original `chatgpt_source`; the importer does not reveal or reconstruct unavailable internal reasoning. The displayed text is the summary/content supplied by the export, not a claim of complete model thought traces.

No provider-native `output` array, encrypted reasoning state, or Responses API reasoning IDs are invented. Subsequent model replay depends on Open WebUI and the provider; this is an archival/UI conversion, not restoration of a provider's private session state. No global/model reasoning settings are changed. `--layout-rendering source` disables layout conversion only, not reasoning conversion.

For already-imported chats, activity/reasoning mode changes the rendering digest. Use the same journal with `--replace-imported-chats` to apply it with source dates, after backing up any server-only changes. The activity defaults will make the replacement payload differ from previous imports, allowing an explicit replacement run; do not change options during an incomplete replacement recovery. See the replacement limits above.

## Search imported chats

Current Open WebUI has built-in history search; a separate vector index is not required for its text search.

- **Manual:** click sidebar Search or press `Cmd+K` / `Ctrl+K`. Search titles and message content. Use the Location filter to choose a project folder; use `archived:true` to find archived chats.
- **Model-driven:** use a tool-capable model with Native Function Calling, enable its Builtin Tools capability, and enable the Chat History category where that setting is available. The model can then call `search_chats` and `view_chat`. Ask it to search your history for a topic, read the matching chats, and summarize with chat references.

This is text retrieval followed by model reading, not automatic semantic/vector search over your whole archive. Imported visible message content is available to history search; hidden/tool-only source data and attachment binaries should not be assumed searchable. Models do not automatically load every project chat just because they are in the same folder.

UI labels and features vary by Open WebUI version. See upstream [History & Search](https://docs.openwebui.com/features/chat-conversations/chat-features/history-search) and [Tools](https://docs.openwebui.com/features/extensibility/plugin/tools/). The importer does not change model capabilities or tool settings on your server.

## Memories and custom instructions

These imports are opt-in and require an export directory with `memories/memories.json` and/or `settings/custom_instructions.json`.

```sh
# Preview memories and a reusable custom-instructions prompt:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --only-extras --include-memories --instructions prompt

# Upload them without importing chats:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --only-extras --include-memories --instructions prompt --upload

# Instead, apply instructions as your account's system prompt:
uv run python openwebui_import.py chatgpt_export_oct_2026 \
  --only-extras --instructions system --upload
```

Omit `--only-extras` to import conversations as well. `--limit` applies only to conversations, not extras.

- **Memories:** each saved memory becomes a user memory. Deleted/removed entries are skipped. The server assigns new creation/update dates; original dates cannot be set through this API. Adding memories invokes the instance's configured embedding service, which may send the text to an external provider. Retrieval behavior depends on the instance's memory configuration.
- **`--instructions prompt`:** creates a private reusable prompt named “ChatGPT custom instructions,” with command `chatgpt-custom-instructions`. It combines the exported name, occupation, about-user text, and response instructions, removing identical duplicate text from legacy/new fields. The complete source instructions are also stored in prompt metadata. This does **not** automatically activate them as a system prompt. An existing command with that name causes an error rather than an overwrite.
- **`--instructions system` (default with `--all`):** updates the combined text in your authenticated account's personal system prompt (`ui.system` through `/api/v1/users/user/settings/update`). An identical prompt is skipped; a different personal prompt is replaced. Other user settings are retained. Global server prompts, shared model prompts, and folder-level instructions are never modified. Existing per-chat or per-model overrides may still take precedence; this does not rewrite historical messages. Avoid changing settings in the UI while the import runs: the API does not provide a compare-and-swap operation. A failed or interrupted settings update can be rerun; the script reads the current value first.

Disabled source custom instructions are rejected. Personality sliders, tool-disable lists, and other ChatGPT-specific settings are not translated into active Open WebUI settings. Inspect the imported text before using it. Memory and prompt creation require the corresponding server permissions; API-key endpoint restrictions may also apply.

Memories and reusable prompts use the same pending/completed journal mechanism as chats, with separate resource scopes. For a pending memory, compare its content in the destination memory store and record its remote ID only after confirming it exists. For a pending prompt, inspect `chatgpt-custom-instructions` and compare its content. Never blindly retry a memory request after an embedding failure: the server may have saved the memory before embedding failed.

## What transfers

- Titles, creation dates, and last-update dates. Database dates use whole Unix seconds, as required by Open WebUI. The original fractional dates remain in the raw source.
- Per-message Unix timestamps, visible user/assistant branches, and the selected branch.
- Original model names, archive state, and starred/pinned state.
- The complete source conversation in the custom `chat.chatgpt_source` field, including tool messages, hidden nodes, source IDs, and metadata. This field is archival data, not Open WebUI's active tool configuration.

If a conversation date is absent, the script derives it from available message dates. Missing message dates fall back to the conversation creation date. Conversations without usable positive dates are rejected rather than dated today. If `current_node` is omitted, the latest visible leaf is selected. Malformed parent trees are rejected.

## What does not transfer

Missing source assets, project-specific instructions, general account settings, and executable tool configuration are not imported. Project folders, available artifact files/Canvas, memories, and custom instructions transfer with the options above or `--all`. Non-text message parts without text become placeholders. Tool and hidden messages remain in the raw source but are omitted from the displayed history; visible descendants reconnect to their nearest visible ancestor. Structured assistant content without a plain-text representation is displayed as JSON.

Original ChatGPT model names may not exist in your Open WebUI instance. Choose an available model before continuing an imported conversation. Without artifact import, raw attachment references are not working Open WebUI file links. Keep the original export and `.chatgpt-assets` directory.

## Authentication failures (HTTP 401/403)

Open WebUI 0.12.4 restricts `GET /api/v1/auths/` to browser session tokens and returns **“A human session is required.”** for a valid API key. The importer recognizes this specific response and resolves the same account ID through `GET /api/v1/users/user/status`. This was verified against a running 0.12.4 instance. No key fingerprint or guessed identity is used for journal scoping.

The fallback requires the user-status feature and API-key access to its route. If unavailable, use an Open WebUI browser session token in `OPENWEBUI_TOKEN`. Other 401/403 responses are not silently bypassed. Identity checks happen before any uploads in that run. Changing a trailing slash on the instance URL does not help: the importer removes it.

For other 403 responses with an API key, ask the administrator to check:

1. The global **API Keys** switch is enabled (current UI: Settings > Admin > Authentication).
2. Your account/group has the **API Keys** feature permission.
3. If API-key endpoint restrictions are enabled, the identity routes `/api/v1/auths/` and `/api/v1/users/user/status` are allowed, along with the routes used by your selected features. Allowlisting `/auths/` does not override its human-session requirement.
4. Your account has the relevant chat-import, folders, memories, prompts, or system-prompt permission.

The importer uses `/api/v1/chats/import`, `/api/v1/memories/add`, `/api/v1/prompts/create`, `/api/v1/folders/` and individual folder routes. Resuming folder assignments also reads individual chat routes; explicit moves use `/api/v1/chats/{id}/folder`. System-prompt mode uses `/api/v1/users/user/settings` and `/api/v1/users/user/settings/update`. Artifacts need `/api/v1/files/` and `/api/v1/notes/create`; replacement needs GET and DELETE access to the journaled `/api/v1/chats/{id}` and permission to delete chats. Endpoint allowance alone does not grant feature permission.

For a 401, check that `OPENWEBUI_TOKEN` contains a valid Open WebUI API key or session token, not an upstream model-provider key. Users can sign in with password or SSO and then create their own Open WebUI API key; imports belong to that account. A separate SSO gateway may require additional authentication not implemented by this script.

Errors include the request method and path, but omit credentials and server response bodies. Do not paste keys into support messages. If a key has been exposed, rotate it and update your environment. After fixing an identity-route failure, rerun the same command with the same journal; do not delete the journal.

## Resume and failures

The default journal is `.local/openwebui-import.sqlite3` (ignored by Git). Use `--state PATH` to choose another location. Keep the same journal when resuming. Entries are scoped to the destination URL, account ID, and source conversation ID. A changed source conversation stops the run rather than creating a second copy or overwriting an imported chat.

Open WebUI assigns a new ID on each import. The script therefore records a pending entry **before** each POST. A timeout, process crash, unexpected response, or date mismatch leaves that entry pending and blocks automatic retry. A server can commit an import before the client receives its response; blindly retrying would create duplicates.

To reconcile a pending entry:

1. Inspect the journal's `imports` table for rows with `remote_id IS NULL`.
2. Inspect/export chats from the destination account. Match `meta.chatgpt_conversation_id` or `chat.chatgpt_source` to the journal's `source_id`.
3. If the chat exists and its dates are correct, set that journal row's `remote_id` to the server's chat ID. If it is definitely absent, delete only that pending row before retrying. Back up the journal before editing it.

Do not delete the whole journal to resolve one failed request. Without it, the script cannot identify earlier completed imports. It also cannot detect chats imported manually or with a different journal. Different URL aliases for the same server count as different destinations.

The script checks dates in the import response, but a dry-run cannot prove that your installed Open WebUI version renders every branch or retains custom fields. Test one chat first. The API contract was checked against upstream [chat routes](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/chats.py) and [chat models](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/chats.py); your server may differ.
