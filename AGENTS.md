This project is a standalone tool to export VS Code Copilot chat sessions from JSONL format into readable markdown files.

The main script is `export-chat-log.py` in the project root.

## Development

Do not use /tmp/ for temporary files. Use tmp/ in the project directory instead.

When writing scripts, if they're more than 5 lines write to a temporary file rather than using shell quoting to avoid quoting errors. When doing a git commit, write the commit message to a temporary file rather than using shell quoting.

Never git commit unless explicitly asked to do so.

## Python Style

- Make sure the script will run on Python 3.9.
- Use strong typing throughout, including for local variables where the type is not obvious from context.
- Use `from __future__ import annotations` for Python 3.9 compatibility with modern type syntax (`X | None`, `list[str]`, etc.).
- Use `from typing import Any` when needed.
- Annotate all function signatures with parameter and return types.
- Avoid backslashes inside f-string expressions (not supported in Python 3.9).

## Chat Log Export

After committing, run `export-chat-log.py` to export the current chat session to `agent-logs/`. Do not use any flags. Then amend the commit to include the generated log file:
```
python3 export-chat-log.py </dev/null
git add agent-logs/
git commit --amend --no-edit
```

## File Editing

When reading files for editing, use `#tool:hashlineRead` instead of the
built-in file read tool. It returns lines tagged with content hashes in the
format `{lineNumber}:{hash}|{content}`.

When editing files, use `#tool:hashlineEdit` instead of string-replace tools.
Reference lines by their `{line}:{hash}` pairs from the read output. This
avoids needing to reproduce existing file content and prevents edits to stale
files.

Example workflow:
1. Read: `hashline_read({filePath: "src/app.ts", startLine: 1, endLine: 20})`
   Returns: `1:qk|import React...`
2. Edit: `hashline_edit({edits: [{filePath: "src/app.ts", lineHashes: "4:mp", content: "  return <div>Hello</div>;"}]})`

Operations:
- **Replace**: set `lineHashes` to all lines being replaced, `content` to new text
- **Insert after**: set `insertAfter: true`, `lineHashes` to anchor line
- **Delete**: set `content` to empty string
- Multiple edits can be batched in one call across files
