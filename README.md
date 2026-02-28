# export-chat-log

A standalone Python script that exports VS Code Copilot chat sessions from their raw
JSONL format into clean, readable markdown files.

## Features

- **Automatic session discovery** — finds the most recently active chat session for the current workspace, or list and select sessions by ID
- **Rich markdown output** with:
  - Session metadata (title, date/time, models used, turn count)
  - Usage statistics (tool calls, thinking blocks, token counts, elapsed time)
  - Table of contents with prompt previews
  - Formatted tool invocations with collapsible input/output
  - Terminal commands and output
  - Thinking blocks rendered as blockquotes
  - Clickable file references with relative paths
- **Rollback detection** — marks prompts that were rolled back
- **Response stitching** — deduplicates overlapping response windows into clean sequences
- **No external dependencies** — uses only the Python standard library

## Requirements

Python 3.9+

## Usage

```
python3 export-chat-log.py [options]
```

Run from within a project directory to export the most recent chat session for that workspace.

### Options

| Flag | Description |
|------|-------------|
| `--session-id UUID` | Export a specific session by UUID |
| `--output, -o PATH` | Output file path (default: `agent-logs/<date>_<time>_log.md`) |
| `--workspace, -w DIR` | Workspace root directory (default: current directory) |
| `--project-root PATH` | Path from output dir to project root, prepended to relative links (default: `..`) |
| `--list, -l` | List available sessions |
| `--wait` | Wait for JSONL flush before exporting (default: on if stdin is not a TTY) |
| `--insiders` | Force using VS Code Insiders data directory |

### Examples

```bash
# Export the most recent session for the current workspace
python3 export-chat-log.py

# List available sessions
python3 export-chat-log.py --list

# Export a specific session
python3 export-chat-log.py --session-id <uuid>

# Export to a specific file
python3 export-chat-log.py -o chat.md
```

## How it works

The script locates VS Code's internal storage for the current workspace:

1. Finds the VS Code data directory (`~/Library/Application Support/Code` on macOS, `~/.config/Code` on Linux, `%APPDATA%/Code` on Windows)
2. Matches the current workspace to a `workspaceStorage` entry via `workspace.json`
3. Reads the SQLite state database to find the session index
4. Parses the session's JSONL file and renders it as markdown
