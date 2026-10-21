# Default web snapshot

Run:

```sh
mise run export
```

The default web export saves conversations, attempts all eight auxiliary snapshots below, and builds the local chat/project HTML index. Conversation format follows `CHATGPT_EXPORT_FORMAT`; auxiliary snapshots always use JSON.

| Snapshot | Local file |
|---|---|
| Saved memories payload (`include_memory_entries=true`) | `memories/memories.json` |
| Custom instructions | `settings/custom_instructions.json` |
| User settings | `settings/settings.json` |
| Beta-feature settings | `settings/beta_features.json` |
| Model catalogue | `settings/models.json` |
| Account metadata | `settings/account.json` |
| Codex usage, if accessible | `settings/codex_usage.json` |
| Compliance response, if accessible | `settings/compliance.json` |

Existing valid snapshots are retained if the corresponding new request is unavailable or fails. `auxiliary-status.json` describes the current attempt, so file presence alone is not proof that the current snapshot succeeded. Progress reports every endpoint and checkpoints saved/unavailable/failed/interrupted/not-attempted outcomes.

## Opt out or override configuration

```sh
# Conversations and local HTML only:
mise run export -- --no-auxiliary

# Explicitly include auxiliary snapshots, even if config disables them:
mise run export -- --auxiliary
```

The default config is `CHATGPT_AUXILIARY = "true"`. To change it, replace that key under the existing `[env]` in private `mise.local.toml`. CLI flags override the config value. Existing scripts using `--auxiliary` still work; no extra flag is needed for new runs.

The example TOML and browser helper's downloaded TOML both show this default and the opt-out. Offline `import`, `index`, and `probe` do not perform auxiliary downloads.

## Availability and failure handling

- HTTP 200: save the raw JSON snapshot.
- Plain HTTP 403/404/405 on an auxiliary endpoint: record it as unavailable with the current account/permissions, retain any previous valid file, and continue with the other endpoints.
- HTTP 401 or an explicit `cf-mitigated: challenge` response: stop the run and preserve the status/checkpoints already written. Remaining endpoints are marked not attempted.
- Other optional request/response failures: record failure and warn. Optional failures/unavailability can coexist with exit status 0, so inspect `auxiliary-status.json` even when the command finishes.

Required conversation-list/detail requests keep their stricter authentication/access-error handling. Treating a forbidden optional endpoint as unavailable does not assert that the credentials are valid for every service.

## Scope limits

These are snapshots of configured, undocumented endpoints, not a verified complete account backup. The memories request is currently a single payload; memory pagination/completeness has not been audited. Attachments, images, audio, generated files, and project-level files/instructions are not separately downloaded. Conversation JSON retains the references returned by the API.

Conversation discovery still follows the configured project and archived-chat scope. The HTML index currently browses chats, not the memories/settings payloads. Auxiliary JSON can contain sensitive account data and remains subject to the same private-output handling as the conversations.
