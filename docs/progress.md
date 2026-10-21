# Progress, logs, and interrupted runs

## Use the default display

```sh
mise run export
mise run import -- /path/to/export.zip
mise run index -- /path/to/previous/export
mise run probe
```

No extra flag is required. The CLI uses Rich for live terminal bars, spinners, completed/total counts, elapsed time, and estimated time remaining. Wider terminals also show percentages and item rates. The layout adapts to narrow terminals; current-operation details appear below the bars rather than displacing the counts.

Progress updates while a network request or retry wait is pending. A request row shows its operation, logical request number, attempt number, and connect/read timeout. Recovery lines distinguish a successful retry followed by another request from one request repeatedly failing. Completion shows HTTP status and decoded response size; this is not compressed wire-transfer bandwidth. Conversation export separately counts bytes written to disk.

Unknown totals remain indeterminate: a cursor-based listing and a directory scan do not have a trustworthy denominator at the start. The exporter reports page numbers, items received, and unique counts until discovery finishes. After that, conversation export and HTML generation use known totals. ETAs change as processing rates change and are not completion guarantees.

## Grouped attachment progress

Attachment work shares one **Attachments** panel instead of leaving a completed status row for every file. It contains:

- An aggregate row: processed work, saved/skipped/failed counts, and distinct references found so far. Other outcome counts appear when nonzero.
- One reusable current-activity row: reference discovery, cache checks, resolution/authentication, transfer, or retry/pacing wait. Byte counts and wide-terminal transfer rates use B/KiB/MiB units. The item ordinal refers to the current conversation, project batch, or saved queue, not the entire account.

The current row is reused across files and hidden between attachment batches. The finished panel collapses to one summary row. Ordinary attachment notices update the panel in place rather than adding console history. Warnings and errors remain visible. Redirected logs use throttled aggregate updates while retaining retry, recovery, and cooldown-stop diagnostics.

The overall attachment total stays unknown while conversations are still being discovered/fetched; a batch reaching its last file does not claim the account is complete. Processed work can exceed distinct references when multiple saved sources examine the same asset. Cached Markdown attachment verification counts as skipped work once per reference in the run. Deferred/interrupted runs keep an unknown overall denominator rather than showing a false 100%.

## Stages and counters

| Stage | Reported information |
|---|---|
| Authentication | Configured bearer versus cookie exchange; whether backend verification still follows |
| HTTP requests | Attempt/max attempts, timeout, response status/size, network failures, retries, and pacing/backoff duration |
| Conversation discovery | Regular/archived pages, reported totals, unique conversations, and deduplication |
| Project discovery | Sidebar pages, discovered project counts, and manual-entry counts |
| Project listings | Project ordinal/count, per-project pages and conversation counts, and additional unique conversations |
| Export/import | Checking/fetching/rendering/writing state; saved, skipped, failed, pending, and bytes written |
| Attachments/artifacts | Reference discovery, checksum verification, resolver attempts, streamed bytes/limits, saved/skipped/missing/failed/partial counts, and inert viewer generation |
| Auxiliary snapshots | Each endpoint, HTTP availability, saved size, and saved/unavailable/failed/interrupted/not-attempted counts |
| Local HTML | Candidate source files, discovered chats, ignored non-chat JSON, Markdown reads, rendered pages, warnings, and catalog writing |
| Run completion | Elapsed time, warning count, exit status, and output/result locations |

Repeated IDs are removed from the export inventory before setting its denominator. Incrementally skipped files still advance the completed count. Failed conversations are counted rather than leaving the bar apparently stalled. Authentication/access failures stop the run and preserve earlier checkpoints.

Project labels in progress use ordinals, not private project names. Chat titles, IDs, message text, credentials, user-agent values, session responses, and HTTP response bodies are not logged by progress. Output directory paths and fixed endpoint names may appear. The source snapshots and HTML pages themselves still contain private account data.

## Redirect logs or suppress progress

Progress goes to **stderr**. Results go to **stdout**. Rich does not redirect stdout, and live-mode result output waits until terminal cleanup so redraws cannot consume the JSON result.

```sh
# JSON stdout plus separate timestamped progress when stderr is redirected:
mise run probe > probe-result.json 2> probe-progress.log

# Leave progress visible while saving the JSON result:
mise run probe > probe-result.json

# Suppress progress, but keep final results and errors:
mise run export -- --no-progress
```

Alternatively, set `CHATGPT_PROGRESS = "false"` under the existing `[env]` in private `mise.local.toml`. The browser helper's downloaded TOML documents this setting without changing existing preferences. On failure before a probe result is available, stdout may be empty; check the exit status and stderr rather than treating an empty file as JSON.

Non-terminal stderr uses timestamped lines with stage starts, bounded periodic updates, waits, retries, and final summaries. It does not emit terminal cursor-control sequences. Live terminal details refresh four times per second; enabling progress does not add API requests or change request pacing.

## Auxiliary snapshots are not a full-backup guarantee

Memories, custom instructions, and account/settings snapshots are part of the default web export. Use `--no-auxiliary` or `CHATGPT_AUXILIARY = "false"` to request conversations only. The existing `--auxiliary` flag remains supported and explicitly enables snapshots even if configuration disables them. CLI flags override the config value. The CLI reports when this stage is explicitly disabled; `probe`, offline `import`, and `index` never fetch auxiliary data.

The auxiliary bar counts endpoint attempts, not individual memories, attachments, or a verified account-wide backup. `8/8` means eight configured endpoints were attempted. It does not mean every endpoint was available or every memory was retrieved.

`auxiliary-status.json` is atomically checkpointed before and after attempts. Each entry includes `attempted`, `available`, and an `outcome`:

- `not_attempted`: the run did not reach this endpoint.
- `in_progress`: the last checkpoint was written just before the request; an ungracefully killed process may leave this state.
- `saved`: a successful snapshot was written.
- `unavailable`: the optional endpoint returned plain 403/404/405 and is not accessible with the current account/permissions; an explicit browser challenge is not classified this way.
- `failed`: a request/response or local write failed.
- `interrupted`: a graceful interruption stopped the active snapshot.

If a new attempt fails, an earlier valid snapshot is retained; its presence alone is not evidence that the current attempt succeeded. A 401 or explicit browser challenge aborts the auxiliary stage and run. Plain 403 on an optional snapshot is recorded as unavailable and the next endpoint is checked; it is not assumed to be a global authentication failure. Other optional endpoint failures are reported as warnings and can coexist with exit status 0, so inspect the availability file even when the command finishes.

## Bounded retry stops

The default single retry-wait cap is 10 seconds, with 30 seconds of cumulative retry sleep shared across the command. The per-request retry default is 2 (3 attempts). A valid server `Retry-After` is respected or causes a clean deferred stop; it is not shortened to force an early retry. Successful requests do not reset the run-wide budget. A rate-limit/retry stop exits **75**, preserves completed files/checkpoints, and leaves later items unrequested rather than cycling through hundreds of fresh retry budgets. See [retry policy](retries.md).

The budgets limit retry sleep, not the entire runtime. Normal pacing, transfers, and local rendering can take longer. The terminal reports deferred stages explicitly; no live account requests were added to diagnose the reported retry loop.

## Stop and resume

Press **Ctrl+C** to stop. The CLI cleans up the live display, reports interruption, and exits **130**. Conversation files and their incremental checkpoints already saved remain available. On rerun, matching ID/format/update-time/checksum entries are skipped unless `--refresh` is supplied. Do not run concurrent writers into the same directory.

Auxiliary status distinguishes endpoints that failed or were interrupted from ones never attempted. Checkpointing requires a writable filesystem; abrupt process termination or filesystem failure cannot guarantee a final status update. Auxiliary snapshots are refreshed on each later web export unless explicitly disabled; they are not treated as conversation checkpoints. Attachment references have their own durable queue and checksums under `.chatgpt-assets/manifest.json`; reruns can recover missing files without refetching unchanged JSON conversations. See [attachments](attachments.md).
