# Contributing

## Set up

Follow [installation](README.md#install), then:

```sh
mise run deps
mise run check
mise run secrets
```

Python and uv are pinned in mise; uv owns `.venv` and `uv.lock`. Gitleaks is pinned for history checks. Do not update global mise configuration or add real credentials to CI.

Tests disable requests-based network access and clear inherited `CHATGPT_*` variables. Add controlled transport fixtures rather than depending on your account. Do not make live ChatGPT requests from tests or change snapshots to silence a failure you have not understood.

## Report a bug without exposing your archive

Include:

- The command, OS, Python version, and commit/version in use.
- Whether the source is live web mode, an official ZIP/JSON import, or an older local export.
- The exit status and relevant summary counts.
- A minimal synthetic input or redacted status/shape report that reproduces the issue.
- Expected behavior and actual behavior.

Do **not** include cookies, tokens, session JSON, account/conversation/file identifiers, raw request headers, HAR files, signed URLs, private paths, conversation text, project names, screenshots containing account data, or export/backup files. The browser helper's downloaded TOML is a credential file, not a diagnostic attachment.

A 100% progress bar means work was attempted, not that every asset was recovered. Exit 1 with zero conversation failures may be an incomplete attachment snapshot. Include outcome counts rather than private manifest records. See [progress](docs/progress.md) and [retries](docs/retries.md).

## Make a change

Keep changes focused, add a regression test at the behavior's actual call site, and run offline gates. Configuration additions need matching defaults/documentation in `mise.local.toml.example`; tests enforce coverage. Preserve legacy export source files, checkpoint safety, safe URL/redirect rules, and stdout/stderr separation.

When changing locks, use the pinned tools and commit both the relevant manifest and lockfile. CI runs offline gates plus secret scanning. Review the staged diff for private data before pushing; a scanner cannot recognize every private sentence or identifier.

For a vulnerability or credential exposure, follow [SECURITY.md](SECURITY.md), not a public bug report.
