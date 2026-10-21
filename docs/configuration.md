# Configuration reference

Settings are read from environment variables. With mise, place them under the existing `[env]` in ignored `mise.local.toml`; see [authentication](authentication.md) for credential setup. Direct uv/Python use must supply environment variables itself: Python does not parse TOML.

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


## Command-line precedence

`--output`, `--format`, `--[no-]attachments`, and `--[no-]auxiliary` override their corresponding environment settings. `--no-index` suppresses automatic HTML generation, and `--no-progress` suppresses progress only. `--refresh` bypasses conversation and attachment caches.

`index`, `zip`, and `unzip` operate offline and do not validate authentication settings. ZIP backups default to a sibling `<output-directory>.zip`; restore defaults to a new sibling `<output-directory>_restored`. See [backup and restore](backups.md).
