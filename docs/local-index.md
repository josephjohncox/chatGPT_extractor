# Offline chat and project index

## Build and open

```sh
# Directory configured by CHATGPT_OUTPUT_DIR in private mise.local.toml:
mise run index

# Any existing export directory (no re-export or credentials required):
mise run index -- /path/to/previous/export

# Equivalent explicit override:
mise run index -- --output /path/to/previous/export

# macOS; other platforms can open index.html in their normal browser:
open /path/to/previous/export/index.html
```

The command writes `index.html` plus readable pages under `.chatgpt-index/chats/`. All links are relative. Keep these generated pages with the original directory if you move it. No HTTP server, network access, or third-party browser assets are required. `index` reads only the directory setting, so expired/malformed API credentials cannot block browsing local files.

`export` and `import` also build the index by default. Set `CHATGPT_BUILD_INDEX = "false"` under `[env]` or pass `--no-index` to disable this. Rebuilding HTML does not use or update `.export-index.json` and does not modify any input JSON/Markdown file. The generated view is a snapshot, not a live filesystem browser.

## What is indexed

- Per-conversation JSON files with a `mapping` object, including exports from older versions of this tool. No incremental manifest or modern ID-hashed filename is required.
- Official `conversations.json` arrays, and JSON objects containing a `conversations` array.
- Markdown files (`.md` or `.markdown`), including older title-only filenames. The first heading supplies the title; otherwise the filename is used. Standard Created / Last updated labels are recognized.
- Nested directories are project/folder labels. Files at the root and `_Uncategorized` use Uncategorized. Folder ID suffixes are retained so two same-named projects are not merged accidentally. A UUID-named old folder stays a UUID; no speculative remote lookup is performed.

Known auxiliary `settings/` and `memories/` trees at the root, hidden trees/files, `node_modules`, generated HTML, and JSON without conversations are not listed as chats. Source symlinks are not followed. Files larger than 64 MiB and unreadable/invalid conversation files are reported as warnings rather than read without bounds.

Chats with the same conversation ID are grouped, even across renamed files/folders. Metadata and readable text come from the newest available update timestamp, not filesystem modification time; all source file links are retained. A same-stem JSON/Markdown pair is one chat. Markdown-only files without IDs remain separate by relative path, because guessing identity from a title could discard distinct chats. Thus the number of index rows can be less than the number of source files.

## Browsing and rendering

The index has a folder sidebar, project selector, title/folder/filename search, date/title/project sorting, counts, and links to both readable pages and original files. Search is metadata-only, not full-text conversation search. With JavaScript disabled, the entire table and file links remain available; filtering/sorting requires JavaScript.

JSON pages show the selected `current_node` branch. Archives missing `current_node` use the older first-child fallback. Markdown is displayed as escaped plain text, preserving formatting syntax rather than interpreting it. Both formats avoid executing embedded HTML or loading image/link targets. Non-text assets remain references in the original JSON; images, audio, files, and project attachments are not downloaded or reconstructed.

Numeric timestamps and ISO dates are supported. Legacy Markdown timestamps without a timezone are interpreted as UTC for sorting/display; their original text remains in the source.

A rendering error leaves a page with a warning and source links, rather than making the chat disappear. Other valid chats remain indexed. The command returns nonzero if it reports warnings; inspect the warning section in the generated page.

## Templates and generated output

- `templates/index.html`: catalog layout, styling, and offline search/filter/sort code.
- `templates/conversation.html`: individual chat page layout and styling.
- `local_index.py`: discovery, grouping, safe relative links, and template binding.

Templates use uppercase `{{PLACEHOLDER}}` markers, substituted once. All archive-controlled text and paths are escaped before insertion. There are no external libraries or arbitrary templates from the archive itself. Re-run `mise run index` after editing the repository templates.

Generated files use atomic replacement and POSIX mode 0600; new managed directories use 0700. Existing original export directory permissions are not changed. The generator refuses to overwrite an unrelated `index.html`, use an unowned `.chatgpt-index` directory, or write through managed-directory symlinks. Move conflicting user-owned output aside before building. Old generated chat pages are retained on rebuild rather than deleted; to remove stale pages, remove only the generated `.chatgpt-index` directory and generated `index.html`, then rebuild.

The HTML includes private chat titles and message text. It does not embed configuration credentials, but chat messages may themselves contain secrets. Default export directories and `.chatgpt-index/` are Git-ignored. If the chosen directory is inside a Git repository, ignore the entire directory (including its `index.html` and source files), not just the companion pages. No source JSON/Markdown is uploaded or published by indexing.
