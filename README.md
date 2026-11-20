# Discord Archiver

Archive discord server channels, threads, direct messages, and group DMs to **CSV** and **JSONL** using a user account. Runs are resumable, rate-limit aware, and produce a predictable on-disk layout with manifests you can build tooling against.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)

> [!WARNING]
> **Automating a user account violates Discord's Terms of Service and can result in account termination.** This tool logs in as a user (not a bot) via [`discord.py-self`](https://github.com/dolfies/discord.py-self). Use it only on your own account, only on data you are entitled to export, and at your own risk. The authors accept no responsibility for any consequences. Never share your token.

---

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick start](#quick-start)
- [Usage](#usage)
  - [Options reference](#options-reference)
  - [Detailed per-option help](#detailed-per-option-help)
  - [Examples](#examples)
- [Output](#output)
  - [Directory layout](#directory-layout)
  - [Data schema](#data-schema)
- [Resuming interrupted runs](#resuming-interrupted-runs)
- [Notes and limitations](#notes-and-limitations)
- [Troubleshooting](#troubleshooting)

---

## Features

- **Servers, threads, and DMs**: archive guild text channels, active and archived threads, 1:1 DMs, and group DMs. Guild and DM modes can be combined in a single run.
- **Two output formats**: JSONL (always) and CSV (on by default), written in lockstep with identical rows and part numbers.
- **Flexible targeting**: select channels by ID or name (substring or exact match), and filter DMs by user ID.
- **Range filtering**: limit by ISO-8601 date or by Discord message ID.
- **Resumable**: per-container checkpoints mean an interrupted run picks up where it left off, and repeat runs fetch only new messages.
- **Rich metadata**: author info, edit timestamps, reply targets, author roles, reaction counts, and (optionally) who reacted.
- **Attachment downloads**: optional, with size limits, timeouts, and atomic writes so partial downloads are retried.
- **File splitting**: automatically roll output into `part-NNN` files after a configurable row count.
- **Self-describing output**: manifests for every channel, thread, and server, plus a top-level index and a snapshot of the configuration used.
- **Cross-platform**: Linux, macOS, and Windows, with UTF-8 console handling and long-path warnings on Windows.
- **Built-in targeted help**: combine `-h` with any option for formats, defaults, related flags, and examples.

## Requirements

- Python **3.10** or newer
- [`discord.py-self`](https://github.com/dolfies/discord.py-self) (**not** upstream `discord.py`)
- A Discord user token

## Installation

```bash
git clone https://github.com/JoshuaErskine/discord-archiver
cd discord-archiver

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -U discord.py-self
```

> [!IMPORTANT]
> Install `discord.py-self` only. Having upstream `discord.py` installed in the same environment can cause import conflicts, so use a clean virtual environment.

## Configuration

Secrets live in a `.env` file. Everything else is configured with command-line flags.

Create `.env` containing your token:

```dotenv
DISCORD_TOKEN=your-token-here
```

The script looks for `.env` in the **current working directory** first, then **next to the script**. A `DISCORD_TOKEN` already set in your environment is also honored. If no token is found, the script offers to create a template `.env` for you on first run.

Restrict access to the file and keep it out of version control:

```bash
chmod 600 .env                    # Linux / macOS
# Windows: right-click .env -> Properties -> Security
echo ".env" >> .gitignore
```

## Quick start

```bash
# 1. Find the IDs you need (nothing is archived in this mode)
python archive_discord.py --guild 123456789012345678 --list-channels

# 2. Preview the resolved configuration (no login, no files written)
python archive_discord.py --guild 123456789012345678 --channels general --show-config

# 3. Run the export
python archive_discord.py --guild 123456789012345678 --channels general
```

**Finding a server ID:** enable Developer Mode (*User Settings -> Advanced*), right-click the server icon, and choose **Copy Server ID**.

## Usage

```text
python archive_discord.py [--guild ID] [--dms [LIST]] [options]
```

At least one of `--guild` or `--dms` is required.

### Options reference

#### Target

| Option | Description | Default |
|---|---|---|
| `--guild ID` | Server ID to archive. Optional when `--dms` is set; if both are given, the server is archived first, then DMs. | none |
| `--channels LIST` | Comma-separated channel IDs and/or names. All-digit entries are treated as IDs; everything else as names. Omit to archive every readable channel. | all readable |
| `--dms [LIST]` | Archive DMs and group DMs. With no value, archives all of them. Pass comma-separated **user IDs** to restrict to conversations with those users (group DMs that include them are matched too). | off |
| `--strict` / `--no-strict` | Require exact (case-insensitive) channel-name matches instead of substring matches. IDs always match exactly. | `--no-strict` |
| `--threads` / `--no-threads` | Also archive active and archived threads under each selected channel. Archived-thread pagination can be slow on large servers. | `--no-threads` |
| `--max N` | Cap messages fetched per channel, thread, or DM in one run. Use `all`, `none`, `unlimited`, or `0` for no cap. | unlimited |

#### Range

| Option | Description | Default |
|---|---|---|
| `--after DATE` | Earliest message date (ISO-8601, e.g. `2024-01-15` or `2024-01-15T12:00:00Z`). Naive values are treated as UTC. | none |
| `--before DATE` | Latest message date (ISO-8601). Must be later than `--after`. | none |
| `--message-after ID` | Earliest message ID, inclusive. Overrides `--after` for the lower bound. | none |
| `--message-before ID` | Latest message ID, inclusive. Overrides `--before` for the upper bound. Must be >= `--message-after`. | none |

#### Ordering and content

| Option | Description | Default |
|---|---|---|
| `--oldest-first` / `--newest-first` | Scan direction. Affects resume behavior on interrupted runs, not the final set of messages. Mutually exclusive. | `--newest-first` |
| `--skip-system-messages` / `--no-skip-system-messages` | Omit system events (joins, boosts, pins, renames, thread-created, calls, etc.). Skipped rows still advance the checkpoint. | off |
| `--roles` / `--no-roles` | Capture the author's role list. Costs one member lookup per unique author; use `--no-roles` for speed. Always empty in DMs. | `--roles` |
| `--reactions` / `--no-reactions` | Capture reaction emoji and counts. No extra API requests. | `--reactions` |
| `--reaction-users` / `--no-reaction-users` | Also capture who reacted. **Expensive**: one request per (message, emoji) pair. Implies `--reactions`. | off |

#### Formats and attachments

| Option | Description | Default |
|---|---|---|
| `--csv` / `--no-csv` | Write a CSV alongside every JSONL part. | `--csv` |
| `--attachments` / `--no-attachments` | Download attachment files. The signed CDN URLs in the data expire, so enable this to keep the files. | off |
| `--attachment-max-bytes BYTES` | Skip attachments larger than this. `0` means no limit. | `52428800` (50 MB) |
| `--attachment-timeout SECONDS` | Per-attachment download timeout. Must be > 0. | `30.0` |

#### Pacing

| Option | Description | Default |
|---|---|---|
| `--throttle` / `--no-throttle` | Pause after every `--batch` messages. Best-effort smoothing; Discord's own rate limiting still applies. | `--throttle` |
| `--batch N` | Messages per batch. Also controls how often the checkpoint is written. | `250` |
| `--pause SECONDS` | Pause length between batches. Fractions allowed. | `5.0` |

#### Output

| Option | Description | Default |
|---|---|---|
| `--out-dir DIR` | Output directory, created if missing. | `discord_export` |
| `--progress-every N` | Log a progress line every N messages per container. `0` disables periodic logs. | `100` |
| `--max-per-file N` | Split output into `part-NNN` files of at most N rows. `0` writes a single part per container. | `50000` |

#### Meta

| Option | Description |
|---|---|
| `--show-config` | Print the resolved configuration as JSON and exit. Needs no token, writes no files, and does not contact Discord. |
| `--list-channels` | Log in, list readable server channels (with `--guild`) and/or DM conversations (with `--dms`), then exit. Nothing is archived. |
| `-h`, `--help`, `-help` | Show help. Combine with options for detailed help (see below). |

### Detailed per-option help

In addition to the standard flat help, the script supports **targeted help** for any option. Put `-h` anywhere alongside the options you want explained:

```bash
python archive_discord.py -h                    # full option list
python archive_discord.py --guild -h            # detailed help for --guild
python archive_discord.py -h --dms --threads    # detailed help for several options
```

Each detailed entry covers accepted formats, defaults, interactions with other flags, related options, and examples. `--no-*` forms resolve to their positive counterparts (for example, `--no-roles -h` shows the `--roles` entry).

### Examples

```bash
# Specific channels by name (substring match: "general" also matches #general-chat)
python archive_discord.py --guild 123456789 --channels general,memes

# Exact-name match only
python archive_discord.py --guild 123456789 --channels general --strict

# Include threads
python archive_discord.py --guild 123456789 --threads

# Date range
python archive_discord.py --guild 123456789 --after 2024-01-01 --before 2024-03-31

# Message ID range
python archive_discord.py --guild 123456789 \
    --message-after 1100000000000000000 --message-before 1200000000000000000

# Chronological scan, with attachment downloads
python archive_discord.py --guild 123456789 --oldest-first --attachments

# Fast run: JSONL only, no role lookups
python archive_discord.py --guild 123456789 --no-csv --no-roles

# Capture who reacted to each message (slow)
python archive_discord.py --guild 123456789 --reaction-users

# All DMs and group DMs
python archive_discord.py --dms

# DMs with specific users only
python archive_discord.py --dms 123456789012345678,987654321098765432

# Server and DMs in one run
python archive_discord.py --guild 123456789 --dms

# Discover channels and DM conversations
python archive_discord.py --guild 123456789 --list-channels
python archive_discord.py --dms --list-channels
```

## Output

### Directory layout

Everything is written under `--out-dir` (default `discord_export`):

```text
discord_export/
├── _index.json                          # all roots with message totals
├── _run.log                             # timestamped run log
├── _config.snapshot.json                # resolved config used for the run
├── checkpoint.json                      # per-container resume state
├── <guild-slug>__<guild-id>/
│   ├── _manifest.json
│   └── <channel-slug>__<channel-id>/
│       ├── _manifest.json
│       ├── messages/
│       │   ├── part-001.jsonl
│       │   └── part-001.csv             # when --csv
│       ├── attachments/                 # only with --attachments
│       │   └── <message-id>_<filename>
│       └── threads/
│           └── <thread-slug>__<thread-id>/
│               ├── _manifest.json
│               ├── messages/
│               └── attachments/
└── _dms__<your-user-id>/
    ├── _manifest.json
    ├── dm__<recipient-slug>__<channel-id>/
    │   ├── _manifest.json
    │   ├── messages/
    │   └── attachments/
    └── group__<group-slug>__<channel-id>/
        ├── _manifest.json
        ├── messages/
        └── attachments/
```

Notes on naming:

- Folder slugs are lowercase ASCII with hyphens; non-ASCII characters are dropped. Uniqueness comes from the trailing ID.
- Attachment files are named `<message-id>_<filename>`. A repeated filename on the same message becomes `<message-id>_2_<filename>`, and so on. Filenames are sanitized for Windows and POSIX.
- Output files use LF line endings on every platform.

### Data schema

JSONL and CSV share the same columns, in this order:

| Column | Description |
|---|---|
| `channel` | Channel name (for threads, the parent channel; for DMs, the recipient or group name) |
| `thread` | Thread name, empty if not a thread |
| `channel_id` | ID of the channel, thread, or DM conversation |
| `message_id` | Message ID |
| `message_type` | Message type name (`default`, `reply`, or a system event type) |
| `author_username` | Author's username |
| `author_display_name` | Author's display name |
| `author_id` | Author's user ID |
| `author_is_bot` | Whether the author is a bot |
| `timestamp_utc` | Creation time, ISO-8601 UTC |
| `edited_timestamp_utc` | Last edit time, empty if never edited |
| `content` | Message text (system events get a generated description when empty) |
| `attachment_urls` | Attachment URLs joined with ` \| ` (signed CDN links that expire) |
| `reply_to_message_id` | ID of the replied-to message, if any |
| `roles` | Comma-separated author roles, highest first (guilds only) |
| `reactions` | Reaction counts, e.g. `👍 x3 \| 🎉 x1` |
| `reaction_users` | Per-emoji reactors, e.g. `👍: alice (111), bob (222) \|\| 🎉: ...` |

Each channel, thread, and DM folder also gets a `_manifest.json` with message and part counts, first/last message IDs, scan order, range settings, and the options in effect. Server and DM root manifests aggregate their children, and `_index.json` lists every root.

## Resuming interrupted runs

Progress is tracked per channel, thread, or DM in `checkpoint.json`. Output is flushed before the checkpoint is written, so a hard kill never leaves the checkpoint ahead of what is on disk.

- **Re-running the same command** continues from the saved position, appending to the existing `part-NNN` files rather than starting over.
- **Interrupted newest-first runs** remember the gap that was never fetched, so older messages are not skipped on resume.
- **A run capped with `--max`** counts as finished; the next run continues from where it stopped (newer messages only with `--newest-first`, the next batch with `--oldest-first`).
- **`--batch`** controls checkpoint frequency: larger values mean fewer pauses but more repeated work after a crash.
- To force a full re-export, use a new `--out-dir` or remove the relevant entries from `checkpoint.json`.

## Notes and limitations

- Only channels the logged-in account can view **and** read history in are archived; others are skipped.
- `--threads` walks active and archived threads. Archived-thread pagination happens for every selected channel and can be slow on large servers, which is why it is off by default.
- Authors who have left a server resolve to an empty `roles` value (logged, not fatal).
- Roles are never populated for DMs, and `--roles` has no effect there.
- Attachment URLs in the exported data are signed and expire. Use `--attachments` if you need the files themselves.
- One failing channel (permission error, deleted mid-run, network error) does not abort the export. It is logged and the run continues.
- **Windows:** the script warns when a path nears the 260-character `MAX_PATH` limit. Use a shorter `--out-dir` or enable Windows long-path support.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `Login failed: Discord rejected the token` | The token in `.env` is wrong or expired. Re-check `DISCORD_TOKEN`. |
| `No token found` / `.env` prompt | No `.env` in the working directory or next to the script, and `DISCORD_TOKEN` is not in the environment. |
| `discord_guild_id is not set` | Pass `--guild ID`, or `--dms` for direct messages. |
| `--dms takes numeric user IDs` | `--dms` filters by **user ID**, not by username. Pass `--dms` alone for every DM, and use `--list-channels` to see what exists. |
| `Nothing matched. Check --channels.` | The filter matched no readable channels. Try `--list-channels`, or drop `--strict`. |
| Import or attribute errors from `discord` | Upstream `discord.py` is installed instead of `discord.py-self`. Use a clean virtual environment. |
| Run is very slow | Try `--no-roles`, avoid `--reaction-users`, leave `--threads` off, or increase `--batch` and lower `--pause`. |

The script exits with a **non-zero status** if any container or the export itself failed, so it is safe to use from cron jobs and wrapper scripts. Details are in `<out-dir>/_run.log`.


## Disclaimer

This project is not affiliated with or endorsed by Discord Inc. You are solely responsible for complying with applicable laws, privacy obligations, and Discord's Terms of Service. Exported archives may contain other people's personal data; store and handle them accordingly.
