# Retry limits, rate limiting, and resume

## Why repeated “attempt 2” can look stuck

Retry counts are per logical request. If a 429 is followed by a successful retry, the next conversation/file begins at attempt 1 again. Its next retry is therefore attempt 2 as well. Repeated `next attempt 2/6` did not prove that one TCP request was hanging; the old log omitted the operation and request identity.

The previous defaults allowed five retry sleeps of 59 seconds for a single request: 295 seconds before network time. There was also no shared run-wide sleep budget, so a successful retry could be followed by another minute-long cooldown on each subsequent item. An offline sequence of alternating 429/200 responses reproduced that behavior without contacting the account.

## New defaults

Under the existing `[env]` in private `mise.local.toml`:

```toml
CHATGPT_MAX_RETRIES = "2"
CHATGPT_MAX_RETRY_WAIT = "10"
CHATGPT_MAX_TOTAL_RETRY_WAIT = "30"
```

- `MAX_RETRIES`: retries per request, so 2 means at most 3 attempts.
- `MAX_RETRY_WAIT`: maximum single automatic retry pause, in seconds.
- `MAX_TOTAL_RETRY_WAIT`: cumulative automatic retry-sleep allowance for the command. Metadata, conversation details, auxiliary snapshots, and binary transfers share it. A successful request does not reset it.

Without a valid `Retry-After`, local exponential delays begin at 1s then 2s and cap at 8s when more retries are configured. Numeric and HTTP-date `Retry-After` headers are parsed. A valid server delay is **not shortened**: if it exceeds either configured allowance, the exporter stops instead of retrying before the server asked it to.

These budgets cover retry sleep, not ordinary request pacing, network time, rendering, hashing, or the entire wall-clock runtime. `CHATGPT_TIMEOUT` remains a connect/read timeout, not a strict whole-response deadline. Large streamed files may take longer than that while bytes keep arriving.

Old explicit overrides still apply. If an earlier example left `CHATGPT_MAX_RETRIES = "5"` in your private file, change it to 2 if you want the new per-request default. The new wait caps still apply when their keys are absent.

## Deferred runs exit 75

The command stops with **75** (temporary failure) when:

- A requested retry pause exceeds the single-wait cap.
- The shared retry-sleep allowance would be exceeded.
- The per-request retry limit is exhausted while still receiving 429.
- Network retries are exhausted.

It does not mark one rate-limited conversation failed and then request hundreds more with a fresh allowance. A stopped client is latched against further requests. Rate-limit stops propagate through conversation export, attachment processing, auxiliary snapshots, and probes instead of being swallowed as ordinary optional-item failures.

Saved conversation files and their incremental checkpoints are retained. The attachment queue and auxiliary availability file record attempted failures separately from work not reached. An interrupted binary transfer is never committed as a complete file.

If the server asks for a 59-second cooldown and the single-wait cap is 10 seconds, the command reports the 59-second requirement and exits without a 59-second sleep. **Wait at least the reported server cooldown before resuming**; an immediate restart is not a quota fix.

```sh
# After the server cooldown, resume using the same output directory:
mise run export

# Browse the partial local snapshot without contacting the API:
mise run index
```

Stop a process already running the old code with Ctrl+C before restarting. Editing configuration or updating source does not replace its in-memory policy. Do not run concurrent writers into the same export directory.

## Deliberately allow a slower unattended run

Raise both wait allowances if that is what you want, for example:

```toml
CHATGPT_MAX_RETRIES = "2"
CHATGPT_MAX_RETRY_WAIT = "60"
CHATGPT_MAX_TOTAL_RETRY_WAIT = "600"
```

This permits individual server cooldowns up to a minute and a total of ten minutes of retry sleep. It does not guarantee that a provider will accept the requests afterward. Increasing steady `CHATGPT_REQUEST_DELAY` can reduce bursts; the appropriate rate depends on the service. Lowering a server-provided cooldown is not a supported acceleration mechanism.

## Response and connection cleanup

Retry responses are explicitly closed **before** waiting. Streaming storage sessions are also closed before cooldown rather than holding their response/socket through the sleep. Buffered JSON responses generally release their connections after reading already, so the missing explicit close was not proof of a deadlocked connection.

On real transport exceptions, pooled connections are cleared while session cookies and configured headers are retained. HTTP 429 alone does not trigger blind session replacement or credential changes. Closing sockets cannot be assumed to remove an account/IP/service quota.

## Read the progress log

New retry lines contain the fixed operation label, a logical `request #N`, attempt/max attempts, delay source (`server Retry-After` or local exponential), and cumulative retry-sleep allowance. A retry that receives HTTP 200 emits a recovery line. Connection-pool cleanup and budget stops have explicit messages. Private URLs, conversation/file IDs, credentials, and response bodies are not added to these logs.

The fixes were tested offline with the reported 59-second pattern, alternating 429/200 requests, early resource cleanup, shared metadata/binary budgets, sticky stop behavior, checkpoint preservation, and exit 75. No new live requests were made to diagnose the quota or validate these changes.
