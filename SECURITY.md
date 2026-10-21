# Security and private data

## Report a vulnerability privately

Use this repository's **Security → Advisories → Report a vulnerability** when private reporting is enabled. If that option is unavailable, open an issue asking for a private reporting channel without technical details or private data. Do not publish credentials or a working exploit involving real account data in an issue.

For ordinary compatibility bugs, use a synthetic fixture and the reporting checklist in [CONTRIBUTING.md](CONTRIBUTING.md).

## Treat every generated snapshot as private

This code can be public; its output must not become public by accident. Conversation JSON/Markdown, generated HTML, attachments, memories, custom instructions, account settings, ZIP backups, and availability manifests can contain sensitive information. ZIP backups are not encrypted.

- Keep credentials in ignored `mise.local.toml` or environment variables.
- Do not upload cookies, bearer tokens, `/api/auth/session` responses, HAR files, raw exports, backup ZIPs, or signed storage URLs.
- Use the ignored default output folder or `exports/`. Add ignore rules for other in-repository output paths.
- The browser helper's console report is redacted, but its downloaded TOML is secret. Review even redacted reports before sharing them.
- POSIX files are written with owner-only permissions where supported. Apply equivalent access controls on other platforms. Backups and copies can have different permissions.
- If a credential reaches Git/GitHub, revoke or refresh it first. Removing a current file does not remove history, pull-request refs, cached commits, logs, or downloaded copies.

## Runtime boundaries

Web mode is read-only and uses undocumented ChatGPT backend endpoints. Authentication, rate limiting, and endpoint compatibility are not guaranteed. Server cooldowns are respected or the run stops; this project does not provide a browser-challenge solver.

Attachment transfers validate known hosts and redirects. External storage requests do not inherit ChatGPT cookies or authorization. Signed download URLs are used immediately and excluded from the attachment manifest and progress logs. The archive UI uses inert viewers for potentially executable archived content; do not manually open untrusted archived HTML/code as an alternative to those viewers.

ZIP restore rejects unsafe paths, symlinks/special files, and conflicting entries, and applies size/entry limits. See [backup boundaries](docs/backups.md). No command is a sandbox for arbitrary hostile files or a guarantee of a complete/atomic account snapshot.

## Development checks

```sh
mise run check
mise run secrets
```

CI scans fetched Git history using pinned Gitleaks with redacted diagnostics. It does not use ChatGPT account secrets. Secret scanners do not prove that arbitrary text is non-private: reviewers must also inspect fixtures, URLs, screenshots, generated artifacts, and commit metadata. Keep test data synthetic.
