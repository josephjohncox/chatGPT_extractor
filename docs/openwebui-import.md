# Import chats into Open WebUI

`openwebui_import.py` reads an export directory or a ChatGPT `conversations.json` file. It validates the source without contacting a server unless you pass `--upload`.

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

Chats belong to the authenticated account. The script checks that account's ID before uploading. It sends one conversation per request to `/api/v1/chats/import`; chat import does not call a model, delete chats, or overwrite existing conversations.

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
- **`--instructions system`:** applies the combined text to your account's system prompt. An identical prompt is skipped; an existing different nonempty prompt blocks the operation. Other user settings are retained. Avoid changing settings in the UI while the import runs: the API does not provide a compare-and-swap operation. A failed or interrupted settings update can be rerun; the script reads the current value first.

Disabled source custom instructions are rejected. Personality sliders, tool-disable lists, and other ChatGPT-specific settings are not translated into active Open WebUI settings. Inspect the imported text before using it. Memory and prompt creation require the corresponding server permissions; API-key endpoint restrictions may also apply.

Memories and reusable prompts use the same pending/completed journal mechanism as chats, with separate resource scopes. For a pending memory, compare its content in the destination memory store and record its remote ID only after confirming it exists. For a pending prompt, inspect `chatgpt-custom-instructions` and compare its content. Never blindly retry a memory request after an embedding failure: the server may have saved the memory before embedding failed.

## What transfers

- Titles, creation dates, and last-update dates. Database dates use whole Unix seconds, as required by Open WebUI. The original fractional dates remain in the raw source.
- Per-message Unix timestamps, visible user/assistant branches, and the selected branch.
- Original model names, archive state, and starred/pinned state.
- The complete source conversation in the custom `chat.chatgpt_source` field, including tool messages, hidden nodes, source IDs, and metadata. This field is archival data, not Open WebUI's active tool configuration.

If a conversation date is absent, the script derives it from available message dates. Missing message dates fall back to the conversation creation date. Conversations without usable positive dates are rejected rather than dated today. If `current_node` is omitted, the latest visible leaf is selected. Malformed parent trees are rejected.

## What does not transfer

Attachment binaries, project folders, general account settings, and executable tool configuration are not imported. Memories and custom instructions transfer only with the explicit options above. Non-text message parts without text become placeholders. Tool and hidden messages remain in the raw source but are omitted from the displayed history; visible descendants reconnect to their nearest visible ancestor. Structured assistant content without a plain-text representation is displayed as JSON.

Original ChatGPT model names may not exist in your Open WebUI instance. Choose an available model before continuing an imported conversation. Raw attachment references are not working Open WebUI file links. Keep the original export and `.chatgpt-assets` directory.

## Resume and failures

The default journal is `.local/openwebui-import.sqlite3` (ignored by Git). Use `--state PATH` to choose another location. Keep the same journal when resuming. Entries are scoped to the destination URL, account ID, and source conversation ID. A changed source conversation stops the run rather than creating a second copy or overwriting an imported chat.

Open WebUI assigns a new ID on each import. The script therefore records a pending entry **before** each POST. A timeout, process crash, unexpected response, or date mismatch leaves that entry pending and blocks automatic retry. A server can commit an import before the client receives its response; blindly retrying would create duplicates.

To reconcile a pending entry:

1. Inspect the journal's `imports` table for rows with `remote_id IS NULL`.
2. Inspect/export chats from the destination account. Match `meta.chatgpt_conversation_id` or `chat.chatgpt_source` to the journal's `source_id`.
3. If the chat exists and its dates are correct, set that journal row's `remote_id` to the server's chat ID. If it is definitely absent, delete only that pending row before retrying. Back up the journal before editing it.

Do not delete the whole journal to resolve one failed request. Without it, the script cannot identify earlier completed imports. It also cannot detect chats imported manually or with a different journal. Different URL aliases for the same server count as different destinations.

The script checks dates in the import response, but a dry-run cannot prove that your installed Open WebUI version renders every branch or retains custom fields. Test one chat first. The API contract was checked against upstream [chat routes](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/chats.py) and [chat models](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/chats.py); your server may differ.
