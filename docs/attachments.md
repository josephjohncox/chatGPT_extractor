# Attachments, images, generated files, and Canvas

## Download with the default snapshot

```sh
mise run export
```

Attachment handling is enabled by default. It downloads referenced files and materializes supported Canvas history in addition to the normal conversation and auxiliary snapshots.

To disable automatic attachment work, pass `--no-attachments` or set `CHATGPT_ATTACHMENTS = "false"` under the existing `[env]` in private `mise.local.toml`. `--attachments` explicitly enables it. `CHATGPT_MAX_ATTACHMENT_BYTES` sets the per-file limit: 536870912 bytes (512 MiB) by default, with allowed values from 1 byte through 2 GiB. `--refresh` forces re-download/re-materialization instead of accepting matching local checksums.

## Backfill an existing directory

```sh
mise run attachments -- /path/to/previous/export
```

This scans saved JSON and downloads its referenced assets. It does not enumerate account conversations again or replace the source JSON/Markdown. It rebuilds the local index afterward unless `--no-index` is supplied. The explicit command works even if automatic attachment handling is disabled in config; do not combine the command with `--no-attachments`.

Modern JSON exports also perform attachment checks on unchanged/skipped conversations during a normal rerun. Modern Markdown exports use the attachment manifest to skip verified assets; missing/incomplete inventories may require fetching the conversation detail again. Markdown-only legacy exports without IDs/raw mappings cannot reliably recover server references. If their manifest already contains references, backfill can resume those; missing Canvas text still needs the raw mapping.

## Supported references

- Uploaded files in message `metadata.attachments` and structured file-ID fields.
- `file-service://file-…` and `sediment://file_…` pointers, including rendered sediment references that place the canonical file ID before a second fragment separator.
- Nested image/audio asset pointers with supported file IDs.
- Generated download links such as `sandbox:/mnt/data/report.csv`, scoped to their conversation and message. Different messages can contain different versions of the same sandbox path.
- Project uploads returned in sidebar project-discovery metadata. With automatic project discovery disabled, the exporter does not invent an account-wide project-file inventory.
- Canvas `canmore.create_textdoc` / `canmore.update_textdoc` content on the selected conversation branch.

File/image references are collected across the raw conversation mapping, not only the displayed message branch. Arbitrary external URLs, citation images, and unrelated website files are not fetched. Unrecognized asset references are recorded as unsupported rather than converted into arbitrary network requests. Project instructions and unreferenced account-library files are not separately inventoried by this feature.

## Authentication and transfer boundaries

A scoped live test found that some file resolvers return relative `/api/library/files/...` URLs. Those content routes can require the ChatGPT session cookie even when the conversation API accepts a bearer token. The downloader therefore supports bearer plus single/split session cookies for approved ChatGPT content paths. If a library request gets 401, the error explains how to add or refresh cookies from the same signed-in account/workspace. See [authentication instructions](authentication.md).

The browser helper cannot read HttpOnly cookies; copying an access token does not copy the cookie session. Keep the bearer token configured if preferred and add exactly one cookie format: either `CHATGPT_SESSION_TOKEN`, or a complete `_0`/`_1` pair. This does not change bearer precedence for conversation API authentication and does not introduce automatic credential fallback.

Signed URLs are obtained through authenticated resolvers, used immediately, and not added to the attachment manifest or progress logs. Transfers accept HTTPS only, reject userinfo/nonstandard ports/encoded traversal, and manually validate each redirect (maximum three). Approved destinations are ChatGPT's known content routes, `oaiusercontent.com` hosts, and a small explicit set of OpenAI Azure storage hosts. Unrecognized hosts fail closed; there is no arbitrary-host override.

Redirected storage requests use fresh sessions without ChatGPT authorization/account headers, cookies, session auth, or netrc/environment authentication. Those fresh sessions also do not inherit environment proxy settings. ChatGPT credentials are never forwarded to a storage host. Transfer sizes are enforced while streaming, including decoded responses without a trusted length. Truncation, exhausted retries, and size-limit failures do not commit partial binary files or replace earlier valid ones.

## Storage, integrity, and partial results

```text
<export>/
├── .chatgpt-assets/
│   ├── .managed
│   ├── manifest.json
│   ├── files/<reference-hash>--<safe-name>
│   └── canvas/<reference-hash>--<name> + operation-history JSON
└── .chatgpt-index/
    ├── assets.html
    ├── asset-views/<hash>.html
    └── chats/…html
```

The manifest records references, conversation/project/message scope, associated source paths, outcome, local path, byte count, and SHA-256. IDs distinguish same-named files. Files are atomically replaced, use POSIX mode 0600, and new managed directories use 0700. Owned-path checks reject symlinks and escaping destinations. The hidden assets directory is Git-ignored; ignore the entire export directory too when using a custom location inside a repository.

Existing files are reused only after size and SHA-256 checks. Queued references are checkpointed before transfer; each completed/failed/interrupted result is checkpointed afterward. On Ctrl+C, completed files and the queue remain available for a rerun. Previous valid copies are retained when a new attempt fails. No deletion of old asset versions is performed.

Outcomes include `saved`, `not_attempted`, `in_progress`, `unavailable`, `failed`, `unsupported`, `partial`, and `interrupted`. HTTP 403/404/405/410 can indicate a file that is inaccessible, deleted, or expired. A 401 or explicit browser challenge stops the run; other missing/failed references are retained in the manifest while remaining work continues. Attachment gaps produce a nonzero partial-snapshot result even when the conversation itself was saved. Use the manifest rather than assuming an existing file proves the newest attempt succeeded.

## Canvas reconstruction

Canvas text is already present in create/update tool-call payloads and does not require executing generated code. Creation text and supported patches are replayed in selected-node ancestry order. Tool responses/metadata supply document IDs where available. Multiple possible targets are not guessed. Operation histories are kept alongside the materialized text so partial results remain inspectable.

Whole-document rewrites replace content once, avoiding duplicate replacements from an empty regex match. Targeted Python-compatible patches use the `regex` engine with a per-patch timeout, pattern/content/patch-count limits, and a conversation replay time budget. Unmatched, invalid, ambiguous, or over-budget updates produce an explicitly partial result containing the last known text and raw operation history. A later complete rewrite can recover known content. Manual Canvas UI edits or server-only state not represented in the returned tool history cannot be reconstructed by inference.

All branches remain in source JSON, but Canvas materialization follows the selected branch. This is not an assertion that all historical Canvas versions or the server's latest independently edited document have been recovered.

## Offline imports

`import` never contacts the API. It can copy uniquely matched bundled ZIP assets, including generated files, while reading members in place rather than extracting archive paths. Absolute/traversing/symlink members are not used, duplicate candidates are not guessed, and streaming size limits still apply. JSON imports can match file-ID-named files in conventional sibling `files`, `assets`, or `attachments` directories; they do not search arbitrary paths named in messages.

Canvas can be materialized offline from the JSON. Remote-only assets absent from the bundle are recorded as unavailable and make the import partial. To retrieve those later, keep credentials in the private config and run `mise run attachments` on the imported directory.

## Local viewer safety and limits

Chat pages link associated assets, and the main index links a global attachment gallery. Recognized local PNG/JPEG/GIF/WebP files can preview as images. HTML, SVG, code, documents, and archives are not navigated to directly by the archive UI.

Local browsers can ignore a raw file's `download` attribute. To avoid executing an HTML artifact, generated **inert asset viewers** show escaped text and create an `application/octet-stream` Blob only when the user clicks **Download original file**. The viewer's script is fixed application code; archived content is embedded as encoded data, never evaluated. Native Chrome testing verified a synthetic HTML artifact downloaded without executing its script or making an external request.

These browser wrappers are capped at 16 MiB per file to bound duplication and browser memory. Larger originals are still saved and reported in the gallery, but must be accessed from the local export directory. Derived views contain private data and should be kept with the archive or regenerated; preserve both hidden directories when moving it. Opening an original executable/HTML file outside this UI remains a user action, not something the exporter performs.

Scoped live checks verified one uploaded raster image and one sandbox-generated binary. The file resolver and sandbox routes remain undocumented and may change. These checks and the controlled transport/UI tests do not prove that every expired historical attachment, asset format, or account plan is recoverable.
