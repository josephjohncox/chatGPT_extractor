# Back up and restore an output directory

These commands are offline. They do not authenticate, fetch conversations, or delete the source directory or input ZIP. They use `CHATGPT_OUTPUT_DIR` when no source is supplied.

## Create a ZIP

Stop any process writing into the export directory first. Do not back up and export concurrently.

```sh
mise run zip
```

For `CHATGPT_OUTPUT_DIR = "exports/personal"`, this creates `exports/personal.zip` and keeps `exports/personal/`. All files, hidden `.chatgpt-assets/` and `.chatgpt-index/` contents, checkpoints, and empty directories are included. Members are relative to the source directory; there is no extra top-level folder.

Choose explicit paths when needed:

```sh
mise run zip -- /path/to/export --output /path/to/backup.zip
```

An existing ZIP is not overwritten by default. To replace it deliberately:

```sh
mise run zip -- --overwrite
```

The replacement is published after compression succeeds. Earlier backups and source files survive a compression failure. The command rejects symlinks/special files, an archive destination inside its own source, and credential/Git-checkout paths such as `.env`, `mise.local.toml`, and `.git`. This helps catch accidentally archiving the checkout instead of its output.

The backup is **not encrypted**. POSIX backup permissions are 0600. Filenames, conversation content, attachments, and account snapshots remain private data. Store the ZIP accordingly. ZIP outputs are Git-ignored; do not force-add them.

## Restore without replacing your working export

```sh
mise run unzip
```

For the example above, this reads `exports/personal.zip` and restores into **`exports/personal_restored/`**. It keeps both the ZIP and the original `exports/personal/`.

To choose another location:

```sh
mise run unzip -- /path/to/backup.zip --output /new/restore-directory
```

The destination must not exist. Restore never merges into an existing export, and `--overwrite` is intentionally unavailable for unzip. On ordinary errors or Ctrl+C, temporary extraction data is removed; the source ZIP is retained. Abrupt termination/power failure can leave a hidden `.restore-*` staging directory, or a partial destination if interrupted during final publication. Inspect it before reuse.

Restore validates all entry paths before extracting. It rejects traversal/absolute paths, Windows drive/stream/reserved paths, duplicate/case/Unicode-colliding paths, file/directory conflicts, encrypted entries, symlinks, and special files. Extracted POSIX files use 0600 and directories 0700.

The default extraction budget is **64 GiB** and **100,000 entries**. To raise the size allowance for a trusted large backup:

```sh
mise run unzip -- /path/to/backup.zip --output /new/location --max-unpacked-gib 128
```

Size limits bound extraction, not every possible resource cost of parsing an untrusted ZIP. Use these tools for your own snapshots, not as an unrestricted archive-processing service.

## Verify the restored snapshot

```sh
mise run index -- /new/restore-directory
```

Open the reported `index.html`. Existing source files are preserved while the derived viewer is rebuilt. A ZIP preserves the source snapshot's existing gaps: missing attachments are still missing after restore, and a successful ZIP does not certify account-wide completeness. Keep the complete restored tree, including its hidden directories.
