#!/usr/bin/env python3
"""
Archive Discord channel history to CSV + JSONL using a user account.

Requires discord.py-self (NOT upstream discord.py):
    pip install -U discord.py-self

Cross-platform: works on Linux, macOS, and Windows. On Windows, if your
out_dir sits deep under a long path, you may approach the classic 260-char
MAX_PATH limit; the script warns when a container's part files are near it.
Enable Windows long-path support or use a shorter --out-dir to avoid it.

Secrets:  .env  -> DISCORD_TOKEN only (gitignored). Looked up in the working
          directory first, then next to this script.
Config:   command-line flags only. If .env is missing, you will be offered
          a template file on first run.

Output layout (per run, under out_dir):
    _index.json
    _run.log
    _config.snapshot.json
    checkpoint.json
    <guild-slug>__<guild-id>/
        _manifest.json
        <channel-slug>__<channel-id>/
            _manifest.json
            messages/part-NNN.jsonl (+ .csv when --csv)
            attachments/<message-id>_<filename>   (only when --attachments)
            threads/
                <thread-slug>__<thread-id>/
                    _manifest.json
                    messages/part-NNN.jsonl (+ .csv)
                    attachments/...
    _dms__<your-user-id>/
        _manifest.json
        dm__<recipient-slug>__<channel-id>/
            _manifest.json
            messages/part-NNN.jsonl (+ .csv)
            attachments/...
        group__<group-slug>__<channel-id>/
            _manifest.json
            messages/part-NNN.jsonl (+ .csv)
            attachments/...

DM mode is enabled with --dms (optionally with a comma-separated list of
user IDs to filter). Guild mode and DM mode can be combined in one run.

WARNING: automating a user account violates Discord's ToS and risks account
termination. See README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp
import discord

# Windows: force UTF-8 console output so emoji/CJK channel names don't crash
# print(). Harmless on POSIX. Falls back silently on exotic consoles.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Old consoles or non-standard streams may not support reconfigure.
            pass

# ---------------------------------------------------------------------------
# Built-in defaults (fallbacks when a flag is omitted)
# ---------------------------------------------------------------------------

# Centralized default configuration. Every CLI flag maps to one of these keys.
# When a flag is not passed on the command line, the value here is used as-is.
DEFAULTS: dict[str, Any] = {
    "discord_guild_id": 0,
    "discord_channels": "",
    "dms": None,                            # None = DM mode off; "" = all DMs
    "strict_match": False,
    "include_threads": False,               # default: off (avoids archived-thread pagination)
    "max_messages_per_channel": None,       # None = unlimited
    "respect_rate_limits": True,
    "messages_per_batch": 250,
    "pause_after_batch_seconds": 5.0,
    "out_dir": "discord_export",
    "progress_every_n_messages": 100,
    "date_after": None,
    "date_before": None,
    "message_id_after": None,
    "message_id_before": None,
    "max_messages_per_file": 50000,         # 0/None = no split
    "oldest_first": False,                  # default: newest -> oldest
    "skip_system_messages": False,
    "include_roles": True,                  # default: on (one member fetch per unique author)
    "write_csv": True,
    "download_attachments": False,
    "attachment_max_bytes": 52_428_800,     # 50 MB
    "attachment_timeout_seconds": 30.0,
    "include_reactions": True,
    "include_reaction_users": False,
}

# Column order used by both CSV headers and the JSONL row builder. Keeping
# this as a module-level constant means the CSV and JSONL schemas can never
# drift apart.
FIELDNAMES: List[str] = [
    "channel", "thread", "channel_id", "message_id", "message_type",
    "author_username", "author_display_name", "author_id", "author_is_bot",
    "timestamp_utc", "edited_timestamp_utc", "content",
    "attachment_urls", "reply_to_message_id", "roles",
    "reactions", "reaction_users",
]

# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------
# These are populated in main() and read/written by helpers throughout the
# module. Using module-level globals keeps signatures simple at the cost of
# testability — acceptable for a one-shot CLI archive tool.

CFG: dict[str, Any] = {}                        # resolved config (defaults + flags)
TOKEN: str = ""                                 # Discord user token
OUT_DIR: Path = Path("discord_export")          # resolved output root
CHECKPOINT_PATH: Path = Path("checkpoint.json") # per-channel last-seen message id
LOG_PATH: Optional[Path] = None                 # _run.log, or None if --show-config
LIST_CHANNELS: bool = False                     # --list-channels mode

# Captured at import time so all elapsed() calls are relative to script start.
_START_MONOTONIC: float = time.monotonic()


def fmt_duration(seconds: float) -> str:
    """Human-readable duration string, e.g. '1h 2m 3s' or '4m 5s'."""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"


def elapsed() -> str:
    """Time since script start, formatted via fmt_duration()."""
    return fmt_duration(time.monotonic() - _START_MONOTONIC)


def log(msg: str) -> None:
    """Print to stdout and append to the run log (if configured). LF-only in file."""
    line = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {msg}"
    print(line)
    if LOG_PATH is not None:
        try:
            # newline="\n" ensures consistent line endings across platforms.
            with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        except OSError:
            # Logging failure must never abort the export.
            pass


def write_text_lf(path: Path, text: str) -> None:
    """Write text with LF-only line endings regardless of OS (3.10+)."""
    path.write_text(text, encoding="utf-8", newline="\n")


def warn_long_path(path: Path) -> None:
    """On Windows, warn if a path approaches the 260-char MAX_PATH limit."""
    if sys.platform != "win32":
        return
    try:
        length = len(os.path.abspath(str(path)))
    except Exception:
        return
    # Leave a small buffer under 260 for suffixes like ".jsonl.tmp".
    if length > 240:
        log(f"Warning: long Windows path ({length} chars, MAX_PATH is 260): {path}")
        log("         Consider a shorter --out-dir or enabling long path support.")


# ---------------------------------------------------------------------------
# .env loader / template helper (stdlib only)
# ---------------------------------------------------------------------------

def _env_path(path: str = ".env") -> Optional[Path]:
    """Find .env in the working directory, then next to this script."""
    for cand in (Path(path), Path(__file__).resolve().parent / path):
        if cand.exists():
            return cand
    return None


def load_env(path: str = ".env") -> None:
    """Minimal .env parser. Sets os.environ keys only if not already set."""
    p = _env_path(path)
    if p is None:
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        # Skip blank lines and comments; require KEY=VALUE form.
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        # Allow "export KEY=VALUE" shell-style lines.
        if key.startswith("export "):
            key = key[len("export "):].strip()
        # Strip surrounding quotes, which are common in .env files.
        value = value.strip().strip('"').strip("'")
        if key:
            # setdefault preserves a token already passed via the environment.
            os.environ.setdefault(key, value)


def ensure_env_file() -> None:
    """
    Ensure a .env file exists (or DISCORD_TOKEN is already in the environment).
    If missing, offer to create a template. Exits when user needs to edit it.
    """
    p = Path(".env")
    if _env_path() is not None:
        return
    if os.environ.get("DISCORD_TOKEN", "").strip():
        return  # token already provided via environment

    print("No .env file found next to this script.")
    print("A .env file is needed to store your DISCORD_TOKEN.")
    try:
        answer = input("Create a template .env file now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # Non-interactive or Ctrl-C — just exit cleanly.
        print()
        sys.exit(1)

    if answer in ("y", "yes"):
        write_text_lf(p, "DISCORD_TOKEN=your-token-here\n")
        if sys.platform == "win32":
            hint = "restrict it via Properties -> Security."
        else:
            hint = "then run: chmod 600 .env"
        sys.exit(
            f"Created {p}.\n"
            f"Add your Discord token to it ({hint}), then run again."
        )

    sys.exit(
        "No token configured.\n"
        "Create .env next to this script containing:\n"
        "    DISCORD_TOKEN=your-token-here"
    )


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def slugify(value: Any, max_len: int = 40) -> str:
    """
    Lowercase ascii-alnum + hyphen slug for folder names.
    Non-ascii and punctuation are dropped; uniqueness is preserved by the ID
    suffix on folder names. Returns 'unnamed' if nothing survives.
    """
    s = str(value or "").lower()
    out: List[str] = []
    prev_dash = False
    for c in s:
        if ("a" <= c <= "z") or ("0" <= c <= "9"):
            out.append(c)
            prev_dash = False
        else:
            # Collapse runs of non-alnum into a single hyphen; skip leading ones.
            if not prev_dash and out:
                out.append("-")
                prev_dash = True
    result = "".join(out).strip("-")
    if not result:
        result = "unnamed"
    # Cap length without leaving a trailing hyphen.
    return (result[:max_len].rstrip("-") or "unnamed")


# Characters forbidden in attachment filenames on Windows and/or POSIX.
_ATTACH_BAD = set('<>:"/\\|?*')

# Windows device names — cannot be used as bare filenames (case-insensitive,
# extension-independent). Prefix with "_" when we hit one.
_WIN_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5",
    "LPT6", "LPT7", "LPT8", "LPT9",
})


def safe_attachment_name(name: Any) -> str:
    """Sanitize an attachment filename for use on disk (Windows + POSIX safe)."""
    s = str(name or "").strip()
    if not s:
        return "attachment"

    # Replace forbidden characters and control codes with underscores.
    chars: List[str] = []
    for c in s:
        if c in _ATTACH_BAD or ord(c) < 32:
            chars.append("_")
        else:
            chars.append(c)
    s = "".join(chars)

    # Windows forbids trailing dots/spaces in filenames. POSIX forbids leading
    # dots being hidden but that's cosmetic — keep them out for consistency.
    s = s.strip(". ")
    if not s:
        return "attachment"

    # Truncate to 100 chars while preserving extension.
    if len(s) > 100:
        if "." in s:
            stem, ext = s.rsplit(".", 1)
            ext = "." + ext[:20]          # cap huge extensions
            s = stem[: max(1, 100 - len(ext))] + ext
        else:
            s = s[:100]
        # Truncation may have left trailing dots/spaces again.
        s = s.rstrip(". ")
        if not s:
            return "attachment"

    # Windows reserved device name. Match on the base name (before any dot).
    if s.split(".", 1)[0].upper() in _WIN_RESERVED:
        s = f"_{s}"

    return s


def folder_name(name: Any, ident: int) -> str:
    """<slug>__<id> for guild, channel, and thread folders."""
    return f"{slugify(name)}__{ident}"


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def parse_date_arg(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 date/datetime string into a timezone-aware datetime."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("none", "null"):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Naive datetimes are assumed to be UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        sys.exit(
            f"Invalid date: {value!r} "
            "(use ISO like 2024-01-15 or 2024-01-15T12:00:00Z)"
        )


# ---------------------------------------------------------------------------
# System-message helpers
# ---------------------------------------------------------------------------

# Message types that represent actual human/bot chat. Anything else is a
# system event (join, boost, pin, thread created, etc.).
_REGULAR_MESSAGE_TYPES = frozenset({"default", "reply"})


def _message_type_name(message: Any) -> str:
    """Return the string name of a message's type, defaulting to 'default'."""
    t = getattr(message, "type", None)
    return getattr(t, "name", "default") if t is not None else "default"


def _is_system_message(message: Any) -> bool:
    """True if this message is a system event rather than normal chat."""
    return _message_type_name(message) not in _REGULAR_MESSAGE_TYPES


def _describe_system_message(message: Any) -> str:
    """Produce a human-readable description for known system-message types."""
    tname = _message_type_name(message)
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    author_name = (
        getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or (f"user {author_id}" if author_id else "unknown")
    )
    mapping = {
        "new_member":                f"{author_name} joined the server",
        "premium_guild_subscription": f"{author_name} boosted the server",
        "premium_guild_tier_1":      "Server reached boost Level 1",
        "premium_guild_tier_2":      "Server reached boost Level 2",
        "premium_guild_tier_3":      "Server reached boost Level 3",
        "channel_name_change":       f"{author_name} changed the channel name",
        "channel_icon_change":       f"{author_name} changed the channel icon",
        "channel_pinned_message":    f"{author_name} pinned a message",
        "thread_created":            f"{author_name} created a thread",
        "thread_starter_message":    "Thread starter message",
        "pin":                       f"{author_name} pinned a message",
        "guild_invite_removed":      f"{author_name} removed an invite",
        "recipient_add":             f"{author_name} added a recipient",
        "recipient_remove":          f"{author_name} removed a recipient",
        "call":                      "Call started",
    }
    # Fallback keeps unknown types visible in the export.
    return mapping.get(tname, f"[{tname}]")


# ---------------------------------------------------------------------------
# Reaction helpers
# ---------------------------------------------------------------------------

def _emoji_str(emoji: Any) -> str:
    """Stable text form for a reaction emoji (unicode or custom)."""
    try:
        s = str(emoji)
    except Exception:
        return "?"
    return s or "?"


def _format_reaction_counts(message: Any) -> str:
    """Join 'emoji xN' pairs into a single pipe-separated string."""
    reactions = getattr(message, "reactions", None)
    if not reactions:
        return ""
    parts: List[str] = []
    for r in reactions:
        try:
            parts.append(f"{_emoji_str(r.emoji)} x{r.count}")
        except Exception:
            # Skip malformed reaction objects rather than failing the row.
            continue
    return " | ".join(parts)


async def _format_reaction_users(message: Any) -> str:
    """
    Build 'emoji: name (id), ...' for each reaction. One API request per
    (message, emoji) pair, so this is expensive; gated by --reaction-users.
    """
    reactions = getattr(message, "reactions", None)
    if not reactions:
        return ""
    blocks: List[str] = []
    for r in reactions:
        emoji = _emoji_str(r.emoji)
        names: List[str] = []
        try:
            # This paginates through every user who reacted with this emoji.
            async for u in r.users():
                display = (
                    getattr(u, "display_name", None)
                    or getattr(u, "name", None)
                    or str(getattr(u, "id", ""))
                )
                uid = getattr(u, "id", None)
                names.append(f"{display} ({uid})" if uid is not None else display)
        except Exception as exc:
            # Permission errors or rate limits — log and continue.
            log(f"    reaction users fetch failed for {message.id}/{emoji}: {exc!r}")
            blocks.append(f"{emoji}: <error>")
            continue
        if names:
            blocks.append(f"{emoji}: " + ", ".join(names))
        else:
            blocks.append(f"{emoji}: <none>")
    # Double-pipe separates emoji blocks; single pipe separates counts elsewhere.
    return " || ".join(blocks)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _max_type(value: str) -> Optional[int]:
    """argparse type for --max: integer, or 'all'/'none'/'0' for unlimited."""
    v = value.strip().lower()
    if v in ("none", "all", "unlimited", "0"):
        return None
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be an integer, or 'all'/'none' for unlimited"
        )
    return n if n > 0 else None


# ---------------------------------------------------------------------------
# Targeted --help support
# ---------------------------------------------------------------------------
#
# Normal argparse help is a single flat list. That is fine for a scan, but the
# detailed help for one flag (formats, defaults, interactions, examples) does
# not fit there. This section adds a second help mode:
#
#   discord_archive.py -h                     -> full argparse help
#   discord_archive.py -help                  -> same (alias)
#   discord_archive.py --guild -h             -> detailed help for --guild
#   discord_archive.py -h --dms --threads     -> detailed help for both
#
# Help is detected before argparse runs, because argparse would otherwise try
# to consume the help flag as the value of the preceding option and error out.

HELP_FLAGS = frozenset({"-h", "--help"})

# Map every --no-* form to its positive counterpart so detail lookups work
# regardless of which form the user asked about.
DETAILED_HELP_ALIASES: Dict[str, str] = {
    "--no-strict": "--strict",
    "--no-threads": "--threads",
    "--newest-first": "--oldest-first",
    "--no-skip-system-messages": "--skip-system-messages",
    "--no-roles": "--roles",
    "--no-reactions": "--reactions",
    "--no-reaction-users": "--reaction-users",
    "--no-csv": "--csv",
    "--no-attachments": "--attachments",
    "--no-throttle": "--throttle",
}

DETAILED_HELP: Dict[str, str] = {
    "--guild": """\
  --guild ID

    Server (guild) ID to archive, as a Discord snowflake — a big integer like
    123456789012345678, not the server name.

    How to find it:
      1. Enable Developer Mode in Discord (User Settings -> Advanced).
      2. Right-click the server icon in the left sidebar.
      3. Choose "Copy Server ID".

    Required for a server export. Optional when --dms is given; if both are
    passed, the server is archived first, then the DMs.

    Default: 0 (not set)

    Related: --channels, --threads, --dms, --list-channels

    Examples:
      discord_archive.py --guild 123456789012345678
      discord_archive.py --guild 123456789012345678 --dms
""",

    "--channels": """\
  --channels LIST

    Comma-separated channel IDs and/or names to include. Server channels only;
    DMs are controlled by --dms.

    Each entry is split on commas. A part that is all digits is a channel ID;
    anything else is a channel name.
      - Default: lowercase substring match. --channels general also matches
        #general-chat.
      - With --strict: exact match. --channels general only matches #general.

    If --channels is omitted, every readable channel is archived. Channels the
    account cannot view or read history on are skipped.

    Default: "" (all readable channels)

    Related: --guild, --strict, --threads

    Examples:
      discord_archive.py --guild 123... --channels general,memes
      discord_archive.py --guild 123... --channels 987654321098765432
      discord_archive.py --guild 123... --channels general --strict
""",

    "--dms": """\
  --dms [LIST]

    Archive direct messages and group DMs.

      --dms                     every DM and group DM
      --dms 111111111111111111  only DMs with user 111... plus any group DM
                                that includes that user
      --dms 111...,222...       any DM/group DM that includes either user

    Filtering is by USER ID, not by the conversation channel ID. Every entry
    must be a numeric user ID; non-numeric entries are rejected so a typo
    cannot widen the selection to "every DM". Use --list-channels to see what
    conversations exist.

    Can be used alone, or with --guild (server export first, then DMs).

    Default: off (None)

    Related: --list-channels, --guild

    Examples:
      discord_archive.py --dms
      discord_archive.py --dms 111111111111111111
      discord_archive.py --guild 123... --dms
""",

    "--strict": """\
  --strict / --no-strict

    When --channels contains names (not IDs), require exact channel-name
    matches instead of lowercase substring matches.

      --no-strict (default)  --channels general matches #general, #general-chat
      --strict               --channels general matches only #general

    Numeric channel IDs are always matched exactly.

    Default: --no-strict

    Related: --channels
""",

    "--threads": """\
  --threads / --no-threads

    Also walk active and archived threads under each selected channel.

    Active threads are cheap. Archived threads require paginating the archive
    endpoint for every selected channel, which can be slow on big servers.
    Off by default for that reason.

    Threads are written to <channel-folder>/threads/<thread-folder>/.

    Default: --no-threads

    Related: --channels
""",

    "--max": """\
  --max N

    Cap messages fetched per channel, thread, or DM conversation in one run.

      --max 5000      at most 5000 messages per container
      --max all       unlimited (also none / unlimited / 0)

    A capped pass counts as finished: later runs resume from where this one
    stopped. With --newest-first that means newer messages only; with
    --oldest-first it means the next batch after the last one.

    Default: unlimited

    Related: --oldest-first
""",

    "--after": """\
  --after DATE

    Earliest message date to include, ISO-8601:
      2024-01-15              (midnight UTC)
      2024-01-15T12:00:00Z    (explicit UTC)

    Naive datetimes are treated as UTC.

    If --message-after is also given, the message-ID bound wins on this side.

    Default: none

    Related: --before, --message-after
""",

    "--before": """\
  --before DATE

    Latest message date to include, same ISO-8601 format as --after. Must be
    later than --after when both are set.

    If --message-before is also given, the message-ID bound wins on this side.

    Default: none

    Related: --after, --message-before
""",

    "--message-after": """\
  --message-after ID

    Earliest message ID to include, inclusive. Discord snowflake integer.

    Takes precedence over --after for the lower bound when both are given.

    Default: none

    Related: --after, --message-before
""",

    "--message-before": """\
  --message-before ID

    Latest message ID to include, inclusive. Must be >= --message-after when
    both are set.

    Takes precedence over --before for the upper bound when both are given.

    Default: none

    Related: --before, --message-after
""",

    "--oldest-first": """\
  --oldest-first / --newest-first

    Scan order through message history.

      --newest-first (default)  newest first, walk backward
      --oldest-first            oldest first, walk forward

    Order affects resume behavior on interrupted runs, not the final set of
    messages. The two forms are mutually exclusive.

    Default: --newest-first

    Related: --max
""",

    "--skip-system-messages": """\
  --skip-system-messages / --no-skip-system-messages

    Omit Discord system events: joins, boosts, pin notifications, channel
    name/icon changes, thread-created messages, calls, invite removals,
    recipient added/removed.

    Regular chat and replies are never skipped. Skipped rows still advance the
    checkpoint, so the next run does not re-process them.

    Default: off (system events are included)

    Related: the "message_type" column in CSV/JSONL
""",

    "--roles": """\
  --roles / --no-roles

    Capture the message author's role list in the "roles" column.

    One member lookup per unique author (cache -> local -> API). On large
    servers this can be slow; --no-roles is the fast path. Users who have
    left the server resolve to an empty list (logged, not fatal).

    Has no effect on DMs: DM rows always have an empty roles column.

    Default: --roles (on)

    Related: --progress-every
""",

    "--reactions": """\
  --reactions / --no-reactions

    Capture reaction emoji and counts in the "reactions" column, formatted as
    "emoji xN | emoji2 xM".

    Does not issue extra API requests; counts are on each message already.

    Default: --reactions (on)

    Related: --reaction-users
""",

    "--reaction-users": """\
  --reaction-users / --no-reaction-users

    Also capture who reacted, in the "reaction_users" column, formatted per
    emoji as "emoji: name (id), name (id) || emoji2: ...".

    Expensive: one API request per (message, emoji) pair, paginating the full
    user list. Can multiply run time significantly. Implies --reactions.

    Default: off

    Related: --reactions
""",

    "--csv": """\
  --csv / --no-csv

    Write a CSV file alongside every JSONL part, with the same rows and the
    same part numbers. Headers are written only when a part is created or
    resumed from an empty file, so a resumed run appends cleanly.

    Default: --csv (on)

    Related: --max-per-file
""",

    "--attachments": """\
  --attachments / --no-attachments

    Download attachment files to <container-folder>/attachments/.

    Filenames are <message-id>_<filename>; a second same-named attachment in
    the same message becomes <message-id>_2_<filename>, and so on. Downloads
    go to .tmp and are atomically renamed, so an interrupted download is
    retried on the next run.

    The "attachment_urls" column still carries the signed CDN links either
    way, but those links expire. Use --attachments to keep the files.

    Default: off

    Related: --attachment-max-bytes, --attachment-timeout
""",

    "--attachment-max-bytes": """\
  --attachment-max-bytes BYTES

    Skip attachments larger than this many bytes. 0 means no limit.

    Default: 52428800 (50 MB)

    Related: --attachments
""",

    "--attachment-timeout": """\
  --attachment-timeout SECONDS

    Per-attachment download timeout. Must be > 0.

    Default: 30.0

    Related: --attachments
""",

    "--throttle": """\
  --throttle / --no-throttle

    Pause after every --batch messages while iterating history. Best-effort
    smoothing, not a substitute for Discord's own rate limiting. It also
    determines how often the checkpoint is written.

    Default: --throttle (on)

    Related: --batch, --pause
""",

    "--batch": """\
  --batch N

    Messages fetched before pausing (when --throttle is on) and before writing
    the checkpoint. Bigger values mean fewer pauses but more repeated work
    after a crash; smaller values mean more pauses but a tighter resume
    window.

    Default: 250

    Related: --throttle, --pause
""",

    "--pause": """\
  --pause SECONDS

    Length of the pause between batches. Fractional values are allowed.

    Default: 5.0

    Related: --throttle, --batch
""",

    "--out-dir": """\
  --out-dir DIR

    Output directory for everything this run writes. Created if missing.

    On Windows, paths approaching the 260-character MAX_PATH limit are logged
    as warnings. Use a shorter --out-dir or enable long path support.

    Default: discord_export

    Related: --show-config
""",

    "--progress-every": """\
  --progress-every N

    Print a progress line every N messages fetched per container. Use 0 to
    disable periodic logs (start/end per container are still printed).

    Default: 100

    Related: --throttle
""",

    "--max-per-file": """\
  --max-per-file N

    Split output after N rows. Each channel, thread, or DM writes part-NNN.jsonl
    (and part-NNN.csv) files of at most N rows, in both formats in lockstep.
    Use 0 for a single part per container.

    Default: 50000

    Related: --csv
""",

    "--show-config": """\
  --show-config

    Print the resolved configuration (defaults + flags) as JSON and exit.

    Does not require a token, does not create output files, does not contact
    Discord.

    Example:
      discord_archive.py --guild 123... --after 2024-01-01 --show-config
""",

    "--list-channels": """\
  --list-channels

    Log in, print readable server channels (with --guild) and/or DM
    conversations (with --dms), then exit. Useful for finding channel and user
    IDs before a real run.

    In this mode the export log and config snapshot are not written; nothing
    is archived.

    Examples:
      discord_archive.py --guild 123... --list-channels
      discord_archive.py --dms --list-channels
      discord_archive.py --guild 123... --dms --list-channels
""",
}


def _get_parser_known_options(parser: argparse.ArgumentParser) -> set:
    """Option strings the parser accepts, excluding the help flags."""
    opts: set = set()
    for action in parser._actions:
        for opt in action.option_strings:
            if opt not in HELP_FLAGS:
                opts.add(opt)
    return opts


def _print_detailed_help(parser: argparse.ArgumentParser, opts: List[str]) -> None:
    """Print DETAILED_HELP for each requested option, falling back to argparse."""
    print()
    for i, opt in enumerate(opts):
        if i:
            print("=" * 72)
            print()
        key = DETAILED_HELP_ALIASES.get(opt, opt)
        detail = DETAILED_HELP.get(key)
        if detail:
            print(detail.rstrip())
        else:
            action = next(
                (a for a in parser._actions if opt in a.option_strings), None
            )
            if action is not None:
                print(f"  {opt}")
                if action.help:
                    print(f"    {action.help}")
            else:
                print(f"  {opt}  (no detailed help available)")
        print()


def _handle_targeted_help(parser: argparse.ArgumentParser, argv: List[str]) -> bool:
    """
    If argv contains a help flag plus at least one specific option, print
    detailed help for those options and exit.

    Returns False when no specific option was requested, so argparse can run
    its normal -h / --help path.
    """
    if not any(a in HELP_FLAGS for a in argv):
        return False

    known = _get_parser_known_options(parser)
    requested: List[str] = []
    for a in argv:
        if a in HELP_FLAGS:
            continue
        # Support --opt=value by looking only at the option name.
        opt = a.split("=", 1)[0]
        if opt in known:
            requested.append(opt)

    if not requested:
        return False

    _print_detailed_help(parser, requested)
    sys.exit(0)


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser with grouped options for readability."""
    p = argparse.ArgumentParser(
        prog="discord_archive.py",
        description=(
            "Export Discord channel history and DMs to CSV + JSONL "
            "using a user account.\n"
            "Requires discord.py-self (a user-account fork of discord.py).\n"
            "\n"
            "For detailed help on a specific option, combine -h with that "
            "option:\n"
            "    discord_archive.py --guild -h\n"
            "    discord_archive.py -h --dms --threads\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Detailed help:\n"
            "  Combine -h with an option to see its full description, formats,\n"
            "  defaults, related flags, and examples:\n"
            "      %(prog)s --guild -h\n"
            "      %(prog)s -h --dms --threads\n"
            "\n"
            "Examples:\n"
            "  %(prog)s --guild 123456789 --channels general,memes\n"
            "  %(prog)s --guild 123456789 --threads\n"
            "  %(prog)s --guild 123456789 --after 2024-01-01 --before 2024-03-31\n"
            "  %(prog)s --guild 123456789 --message-after 1100000000000000000 \\\n"
            "           --message-before 1200000000000000000\n"
            "  %(prog)s --guild 123456789 --oldest-first\n"
            "  %(prog)s --guild 123456789 --attachments\n"
            "  %(prog)s --guild 123456789 --no-csv\n"
            "  %(prog)s --guild 123456789 --no-roles\n"
            "  %(prog)s --guild 123456789 --reaction-users\n"
            "  %(prog)s --guild 123456789 --show-config\n"
            "  %(prog)s --guild 123456789 --list-channels\n"
            "  %(prog)s --dms\n"
            "  %(prog)s --dms 123456789012345678,987654321098765432\n"
            "  %(prog)s --guild 123456789 --dms\n"
            "  %(prog)s --dms --list-channels\n"
        ),
    )

    # ---- target selection ----
    g = p.add_argument_group("target")
    g.add_argument("--guild", dest="discord_guild_id", type=int, default=None,
                   metavar="ID", help="server ID (optional when --dms is set)")
    g.add_argument("--channels", dest="discord_channels", default=None,
                   metavar="LIST", help="comma-separated channel IDs and/or names")
    g.add_argument("--dms", dest="dms", nargs="?", const="", default=None,
                   metavar="LIST",
                   help="archive direct messages. With no value, archives every "
                        "DM/group DM; pass a comma-separated list of user IDs to "
                        "restrict. Can be combined with --guild.")
    g.add_argument("--strict", dest="strict_match",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="require exact channel-name matches")
    g.add_argument("--threads", dest="include_threads",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="also walk threads (default: off; turning this on "
                        "paginates archived threads for every channel)")
    g.add_argument("--max", dest="max_messages_per_channel", type=_max_type,
                   default=None, metavar="N",
                   help="cap messages per channel; 'all' for unlimited")

    # ---- date / id range ----
    g = p.add_argument_group("range")
    g.add_argument("--after", dest="date_after", default=None, metavar="DATE",
                   help="earliest message date (ISO, e.g. 2024-01-15)")
    g.add_argument("--before", dest="date_before", default=None, metavar="DATE",
                   help="latest message date (ISO)")
    g.add_argument("--message-after", dest="message_id_after", type=int,
                   default=None, metavar="ID",
                   help="earliest message ID to include (inclusive)")
    g.add_argument("--message-before", dest="message_id_before", type=int,
                   default=None, metavar="ID",
                   help="latest message ID to include (inclusive)")

    # ---- ordering and content toggles ----
    g = p.add_argument_group("ordering and content")
    order = g.add_mutually_exclusive_group()
    order.add_argument("--oldest-first", dest="oldest_first",
                       action="store_true", default=None,
                       help="scan oldest -> newest (chronological)")
    order.add_argument("--newest-first", dest="oldest_first",
                       action="store_false",
                       help="scan newest -> oldest (default)")
    g.add_argument("--skip-system-messages", dest="skip_system_messages",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="omit join/boost/pin/rename events from output")
    g.add_argument("--roles", dest="include_roles",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="capture the author's role list (default: on; "
                        "disabling avoids one member fetch per unique author)")
    g.add_argument("--reactions", dest="include_reactions",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="capture reaction emoji + count (default: on)")
    g.add_argument("--reaction-users", dest="include_reaction_users",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="also capture who reacted to each message "
                        "(slow: one API request per reaction; implies --reactions)")

    # ---- formats / attachments ----
    g = p.add_argument_group("formats and attachments")
    g.add_argument("--csv", dest="write_csv",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="write CSV alongside JSONL (default: on)")
    g.add_argument("--attachments", dest="download_attachments",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="download attachment files (default: off)")
    g.add_argument("--attachment-max-bytes", dest="attachment_max_bytes",
                   type=int, default=None, metavar="BYTES",
                   help="skip attachments larger than N bytes (0 = no limit)")
    g.add_argument("--attachment-timeout", dest="attachment_timeout_seconds",
                   type=float, default=None, metavar="SECONDS",
                   help="per-attachment download timeout")

    # ---- pacing / rate limits ----
    g = p.add_argument_group("pacing")
    g.add_argument("--throttle", dest="respect_rate_limits",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="apply batch pauses (default: on)")
    g.add_argument("--batch", dest="messages_per_batch", type=int, default=None,
                   metavar="N", help="messages per batch before pausing")
    g.add_argument("--pause", dest="pause_after_batch_seconds", type=float,
                   default=None, metavar="SECONDS", help="pause length between batches")

    # ---- output layout ----
    g = p.add_argument_group("output")
    g.add_argument("--out-dir", dest="out_dir", default=None, metavar="DIR",
                   help="output directory")
    g.add_argument("--progress-every", dest="progress_every_n_messages",
                   type=int, default=None, metavar="N",
                   help="print progress every N messages")
    g.add_argument("--max-per-file", dest="max_messages_per_file", type=int,
                   default=None, metavar="N",
                   help="split output files after N messages (0 for no split)")

    # ---- meta commands ----
    g = p.add_argument_group("meta")
    g.add_argument("--show-config", action="store_true",
                   help="print the resolved configuration and exit")
    g.add_argument("--list-channels", action="store_true",
                   help="log in, list readable channels and/or DMs, and exit")

    return p


# ---------------------------------------------------------------------------
# Config resolution and validation
# ---------------------------------------------------------------------------

def resolve_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Merge built-in defaults with CLI flag values (flags win when present)."""
    cfg = dict(DEFAULTS)
    overrides: List[str] = []
    for key in DEFAULTS:
        value = getattr(args, key, None)
        # None means "flag wasn't supplied" (BooleanOptionalAction uses None
        # as its "unset" sentinel when default=None).
        if value is not None:
            cfg[key] = value
            overrides.append(key)
    if overrides:
        print(f"Overrides from command line: {', '.join(sorted(overrides))}")
    return cfg


def validate_config(cfg: Dict[str, Any]) -> None:
    """Coerce and sanity-check every config value; exit with a clear error."""
    # ---- guild id (required only when not in DM mode) ----
    try:
        cfg["discord_guild_id"] = int(cfg["discord_guild_id"])
    except (TypeError, ValueError):
        sys.exit("discord_guild_id must be a numeric server ID "
                 "(pass --guild).")

    dms_mode = cfg.get("dms") is not None
    if dms_mode:
        # Normalize to string; "" means "all DMs".
        cfg["dms"] = str(cfg.get("dms") or "")
        # A non-numeric entry used to be dropped silently, which could widen
        # "--dms alice" into "archive every DM". Refuse instead.
        bad = [x.strip() for x in cfg["dms"].split(",")
               if x.strip() and not _is_id(x.strip())]
        if bad:
            sys.exit(
                "--dms takes numeric user IDs, comma separated "
                f"(not understood: {', '.join(repr(b) for b in bad)}). "
                "Pass --dms alone to archive every DM."
            )
        if cfg["discord_guild_id"] < 0:
            sys.exit("discord_guild_id must be >= 0.")
    else:
        if cfg["discord_guild_id"] <= 0:
            sys.exit("discord_guild_id is not set. Pass --guild, "
                     "or --dms for direct messages.")

    # ---- positive-integer fields ----
    for key, minimum in (
        ("messages_per_batch", 1),
        ("progress_every_n_messages", 0),   # 0 disables periodic progress logs
    ):
        try:
            cfg[key] = int(cfg[key])
        except (TypeError, ValueError):
            sys.exit(f"{key} must be an integer (got {cfg[key]!r}).")
        if cfg[key] < minimum:
            sys.exit(f"{key} must be >= {minimum}.")

    # ---- pause duration ----
    try:
        cfg["pause_after_batch_seconds"] = float(cfg["pause_after_batch_seconds"])
    except (TypeError, ValueError):
        sys.exit("pause_after_batch_seconds must be a number.")
    if cfg["pause_after_batch_seconds"] < 0:
        sys.exit("pause_after_batch_seconds cannot be negative.")

    # ---- per-channel message cap (None = unlimited) ----
    max_msgs = cfg["max_messages_per_channel"]
    if max_msgs is not None:
        try:
            cfg["max_messages_per_channel"] = int(max_msgs)
        except (TypeError, ValueError):
            sys.exit("max_messages_per_channel must be an integer or null.")

    # ---- per-file message cap (0/None = no split) ----
    max_file = cfg["max_messages_per_file"]
    if max_file is not None:
        try:
            max_file = int(max_file)
        except (TypeError, ValueError):
            sys.exit("max_messages_per_file must be an integer or null.")
        cfg["max_messages_per_file"] = max_file if max_file > 0 else None

    # ---- boolean flags: coerce whatever's in the env/config to bool ----
    for key in (
        "strict_match", "include_threads", "respect_rate_limits",
        "oldest_first", "skip_system_messages", "include_roles", "write_csv",
        "download_attachments", "include_reactions", "include_reaction_users",
    ):
        cfg[key] = bool(cfg[key])

    # --reaction-users without --reactions is almost certainly a mistake:
    # the per-user data is useless if the count row is empty.
    if cfg["include_reaction_users"] and not cfg["include_reactions"]:
        cfg["include_reactions"] = True

    # ---- attachment limits ----
    try:
        cfg["attachment_max_bytes"] = int(cfg["attachment_max_bytes"])
    except (TypeError, ValueError):
        sys.exit("attachment_max_bytes must be an integer.")
    if cfg["attachment_max_bytes"] < 0:
        sys.exit("attachment_max_bytes must be >= 0.")
    try:
        cfg["attachment_timeout_seconds"] = float(cfg["attachment_timeout_seconds"])
    except (TypeError, ValueError):
        sys.exit("attachment_timeout_seconds must be a number.")
    if cfg["attachment_timeout_seconds"] <= 0:
        sys.exit("attachment_timeout_seconds must be > 0.")

    # ---- date range sanity check ----
    a = parse_date_arg(cfg.get("date_after"))
    b = parse_date_arg(cfg.get("date_before"))
    if a and b and a >= b:
        sys.exit("date_after must be earlier than date_before.")

    # ---- message-id range sanity check ----
    for key in ("message_id_after", "message_id_before"):
        value = cfg.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            cfg[key] = None
            continue
        try:
            value = int(value)
        except (TypeError, ValueError):
            sys.exit(f"{key} must be a message ID (integer) or null.")
        if value <= 0:
            sys.exit(f"{key} must be a positive message ID.")
        cfg[key] = value

    a_id = cfg.get("message_id_after")
    b_id = cfg.get("message_id_before")
    if a_id is not None and b_id is not None and a_id > b_id:
        sys.exit("message_id_after must not be greater than message_id_before.")

    # ---- plain strings (fallbacks if somehow None) ----
    cfg["discord_channels"] = str(cfg["discord_channels"] or "")
    cfg["out_dir"] = str(cfg["out_dir"] or "discord_export")


def parse_channel_filter(raw: str) -> Tuple[set, set]:
    """
    Split --channels into (ids, names).
    Numeric parts become IDs; everything else becomes a lowercase name substring.
    """
    ids: set = set()
    names: set = set()
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
        else:
            names.add(part.lower())
    return ids, names


def _is_id(text: str) -> bool:
    """True for a plain ASCII-digit Discord snowflake (rejects '²', '', etc.)."""
    return text.isascii() and text.isdigit()


def parse_dm_filter(raw: str) -> set:
    """Split --dms into a set of user IDs. Empty set means 'all DMs'."""
    ids: set = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if _is_id(part):
            ids.add(int(part))
    return ids


# ---------------------------------------------------------------------------
# Resume state (checkpoint) helpers
# ---------------------------------------------------------------------------
#
# Each container's checkpoint entry is a small dict:
#
#   done : every message with id <= done (inside the requested range) is archived
#   hi   : (interrupted newest-first pass only) the lowest id archived so far;
#          the gap (done, hi) is still missing
#   top  : (with hi) newest id archived by the interrupted pass; once the gap
#          is filled, done jumps to top
#
# A single "highest id seen" mark (the old format) is wrong for newest-first
# scans: an interrupted run would record the newest id, and the resume would
# then skip everything older that was never fetched. Legacy string/int entries
# are still read, and are treated as {"done": id}.

_DISCORD_EPOCH_MS = 1_420_070_400_000


def dt_to_snowflake(dt: datetime) -> int:
    """Smallest snowflake id for a timestamp (used to compare dates with ids)."""
    return (int(dt.timestamp() * 1000) - _DISCORD_EPOCH_MS) << 22


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_scan_state(raw: Any) -> Dict[str, Optional[int]]:
    """Normalize a checkpoint entry (legacy id or dict) to {done, hi, top}."""
    if isinstance(raw, dict):
        state = {k: _to_int(raw.get(k)) for k in ("done", "hi", "top")}
    else:
        state = {"done": _to_int(raw), "hi": None, "top": None}
    if state["hi"] is not None and state["top"] is None:
        state["hi"] = None          # malformed pending marker: ignore it
    return state


def serialize_scan_state(state: Dict[str, Optional[int]]) -> Dict[str, int]:
    """Drop empty fields before writing to checkpoint.json."""
    return {k: v for k, v in state.items() if v is not None}


def next_scan_state(
    prev: Dict[str, Optional[int]],
    oldest_first: bool,
    min_seen: Optional[int],
    max_seen: Optional[int],
    complete: bool,
) -> Dict[str, Optional[int]]:
    """
    Fold one pass over a container into its resume state.

    min_seen / max_seen are the lowest and highest message ids iterated in
    this pass (None if nothing was seen). complete is True when the pass
    finished normally (history exhausted or --max reached), False when it was
    interrupted by an error or Ctrl-C.
    """
    done, hi, top = prev["done"], prev["hi"], prev["top"]
    if max_seen is not None:
        top = max(top, max_seen) if top is not None else max_seen

    if oldest_first:
        if max_seen is not None:
            done = max(done, max_seen) if done is not None else max_seen
        if hi is None:
            # Contiguous upward scan: done alone describes the coverage.
            return {"done": done, "hi": None, "top": None}
        if complete:
            # The gap below an earlier interrupted pass is now filled.
            return {"done": top if top is not None else done, "hi": None, "top": None}
        return {"done": done, "hi": hi, "top": top}

    # newest-first
    if complete:
        return {"done": top if top is not None else done, "hi": None, "top": None}
    if min_seen is not None:
        hi = min(hi, min_seen) if hi is not None else min_seen
    if hi is None:
        return {"done": done, "hi": None, "top": None}
    return {"done": done, "hi": hi, "top": top}


def compute_bounds(
    state: Dict[str, Optional[int]],
    after_id: Optional[int],
    after_dt: Optional[datetime],
    before_id: Optional[int],
    before_dt: Optional[datetime],
) -> Tuple[Any, Any]:
    """
    Return (after, before) for history(); each is None, a datetime, or an
    int message id meaning an *exclusive* bound.

    The resume mark and --message-after combine by taking the stricter one.
    A pending gap (state['hi']) caps the upper end. A message-ID bound beats
    a date bound on the same side, as documented.
    """
    after_ids: List[int] = []
    if state.get("done") is not None:
        after_ids.append(state["done"])
    if after_id is not None:
        after_ids.append(int(after_id) - 1)        # --message-after is inclusive
    after: Any = max(after_ids) if after_ids else after_dt

    before_ids: List[int] = []
    if before_id is not None:
        before_ids.append(int(before_id) + 1)      # --message-before is inclusive
    elif before_dt is not None and state.get("hi") is not None:
        before_ids.append(dt_to_snowflake(before_dt))
    if state.get("hi") is not None:
        before_ids.append(state["hi"])
    before: Any = min(before_ids) if before_ids else before_dt
    return after, before


# ---------------------------------------------------------------------------
# Manifest / index / config snapshot
# ---------------------------------------------------------------------------

def scan_parts(messages_dir: Path) -> List[dict]:
    """Read all part-NNN.jsonl files and return metadata for each."""
    parts: List[dict] = []
    if not messages_dir.exists():
        return parts
    for p in sorted(messages_dir.glob("part-*.jsonl")):
        try:
            # Part number comes from the numeric segment after the first dash.
            num = int(p.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            continue
        rows = 0
        first_id: Optional[str] = None
        last_id: Optional[str] = None
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rows += 1
                    try:
                        mid = str(json.loads(line).get("message_id", "") or "")
                    except json.JSONDecodeError:
                        # Truncated line from a previous crash — keep counting.
                        mid = ""
                    if rows == 1:
                        first_id = mid
                    last_id = mid
        except OSError:
            continue
        parts.append({"part": num, "rows": rows,
                      "first_id": first_id, "last_id": last_id})
    return parts


def write_container_manifest(
    container_dir: Path,
    kind: str,
    container_id: int,
    container_name: str,
    guild_id: int,
    guild_name: str,
    parent_id: Optional[int] = None,
    parent_name: Optional[str] = None,
) -> dict:
    """
    Write _manifest.json for a channel, thread, or DM conversation directory.
    Scans existing part files so totals are accurate across resumed runs.
    """
    parts = scan_parts(container_dir / "messages")
    total_rows = sum(p["rows"] for p in parts)
    first_id = next((p["first_id"] for p in parts if p["first_id"]), None)
    last_id = next((p["last_id"] for p in reversed(parts) if p["last_id"]), None)

    # Count downloaded attachment files (cheap; directory listing only).
    att_dir = container_dir / "attachments"
    att_count = len(list(att_dir.iterdir())) if att_dir.exists() else 0

    # Count child thread folders, if this is a channel.
    thread_count = 0
    if kind == "channel":
        threads_dir = container_dir / "threads"
        if threads_dir.exists():
            thread_count = sum(1 for d in threads_dir.iterdir() if d.is_dir())

    manifest = {
        "kind": kind,
        "id": container_id,
        "name": container_name,
        "guild_id": guild_id,
        "guild_name": guild_name,
        "parent_channel_id": parent_id,
        "parent_channel_name": parent_name,
        "first_message_id": first_id,
        "last_message_id": last_id,
        "total_messages": total_rows,
        "total_parts": len(parts),
        "parts": parts,
        "thread_count": thread_count,
        "attachment_count": att_count,
        "written_formats": ["jsonl"] + (["csv"] if CFG.get("write_csv") else []),
        "scan_order": "oldest_first" if CFG.get("oldest_first") else "newest_first",
        "date_after": CFG.get("date_after"),
        "date_before": CFG.get("date_before"),
        "message_id_after": CFG.get("message_id_after"),
        "message_id_before": CFG.get("message_id_before"),
        "include_roles": CFG.get("include_roles"),
        "include_reactions": CFG.get("include_reactions"),
        "include_reaction_users": CFG.get("include_reaction_users"),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_text_lf(
        container_dir / "_manifest.json",
        json.dumps(manifest, indent=2, ensure_ascii=False),
    )
    return manifest


def write_config_snapshot(out_dir: Path, cfg: Dict[str, Any]) -> Path:
    """Persist the resolved config so a run can be reproduced later."""
    path = out_dir / "_config.snapshot.json"
    write_text_lf(path, json.dumps(dict(cfg), indent=2, ensure_ascii=False))
    return path


# ---------------------------------------------------------------------------
# Output writer (per-container, appends across runs, respects write_csv)
# ---------------------------------------------------------------------------

class ContainerWriter:
    """
    Append-only writer for one channel/thread/DM directory.

    - Resumes from the last existing part-NNN.jsonl, appending to it.
    - Starts a new part file once rows_in_part reaches max_rows.
    - Keeps JSONL and CSV in lockstep (same rows, same part numbers).
    """

    def __init__(self, messages_dir: Path, max_rows: Optional[int],
                 write_csv: bool, fieldnames: List[str]) -> None:
        self.messages_dir = messages_dir
        self.max_rows = max_rows
        self.write_csv = write_csv
        self.fieldnames = fieldnames
        self.part = 1
        self.rows_in_part = 0
        self.csv_f: Optional[Any] = None
        self.jsonl_f: Optional[Any] = None
        self.csv_writer: Optional[csv.DictWriter] = None

        self._scan_existing()
        # If the last existing part is already full, roll over before writing.
        if self.max_rows and self.rows_in_part >= self.max_rows:
            self.part += 1
            self.rows_in_part = 0
        self._open_current()

    def _scan_existing(self) -> None:
        """Locate the highest existing part number and count its rows."""
        if not self.messages_dir.exists():
            return
        existing = sorted(self.messages_dir.glob("part-*.jsonl"))
        if not existing:
            return
        last = existing[-1]
        try:
            self.part = int(last.stem.split("-", 1)[1])
        except (IndexError, ValueError):
            self.part = len(existing)
        try:
            with open(last, "r", encoding="utf-8") as f:
                self.rows_in_part = sum(1 for _ in f)
        except OSError:
            self.rows_in_part = 0

    def _paths(self, part: int) -> Tuple[Path, Optional[Path]]:
        """Return (jsonl_path, csv_path_or_None) for a given part number."""
        base = self.messages_dir / f"part-{part:03d}"
        jsonl = Path(f"{base}.jsonl")
        csv_p = Path(f"{base}.csv") if self.write_csv else None
        return jsonl, csv_p

    def _open_current(self) -> None:
        """Open (or create) the current part's JSONL and CSV files."""
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        jsonl_p, csv_p = self._paths(self.part)
        # newline="\n" -> LF-only JSONL on every OS. Reading uses universal
        # newlines so mixed-ending files from older runs remain parseable.
        self.jsonl_f = open(jsonl_p, "a", encoding="utf-8", newline="\n")
        if csv_p is not None:
            # Write a header only when the file is new/empty (resuming appends
            # should not repeat headers).
            need_header = (not csv_p.exists()) or csv_p.stat().st_size == 0
            # csv module wants newline="" so it can manage its own CRLF/LF.
            self.csv_f = open(csv_p, "a", newline="", encoding="utf-8")
            self.csv_writer = csv.DictWriter(
                self.csv_f, fieldnames=self.fieldnames, extrasaction="ignore"
            )
            if need_header:
                self.csv_writer.writeheader()
        else:
            self.csv_f = None
            self.csv_writer = None

    def _close_current(self) -> None:
        """Flush and close the currently open part files."""
        if self.csv_f:
            self.csv_f.close()
            self.csv_f = None
        if self.jsonl_f:
            self.jsonl_f.close()
            self.jsonl_f = None
        self.csv_writer = None

    def write(self, row: dict) -> None:
        """Append one row to JSONL and (optionally) CSV, rolling parts if needed."""
        if self.max_rows and self.rows_in_part >= self.max_rows:
            self._close_current()
            self.part += 1
            self.rows_in_part = 0
            self._open_current()
        assert self.jsonl_f is not None
        self.jsonl_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if self.csv_writer is not None:
            self.csv_writer.writerow(row)
        self.rows_in_part += 1

    def flush(self) -> None:
        """Flush buffers so progress survives a crash between batches."""
        if self.jsonl_f:
            self.jsonl_f.flush()
        if self.csv_f:
            self.csv_f.flush()

    def close(self) -> None:
        """Close the current part files."""
        self._close_current()


# ---------------------------------------------------------------------------
# Archiver
# ---------------------------------------------------------------------------

class Archiver(discord.Client):
    """
    discord.py-self Client subclass that runs the export in on_ready().

    A class-level member cache avoids refetching the same author repeatedly;
    the HTTP session is reused for attachment downloads.
    """

    _member_cache: Dict[Tuple[int, int], Optional[discord.Member]] = {}
    _http_session: Optional[aiohttp.ClientSession] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures: List[Tuple[str, int, str]] = []   # (label, id, error)
        self._started = False

    async def on_ready(self) -> None:
        """Entry point once the client has logged in."""
        # on_ready can fire again after a gateway reconnect; never start a
        # second export on top of the first.
        if self._started:
            log("Gateway reconnected; export already running, ignoring on_ready.")
            return
        self._started = True
        log(f"Logged in as {self.user} ({self.user.id})")
        try:
            if LIST_CHANNELS:
                await self.dump_channels()
            else:
                ran = False
                if CFG["discord_guild_id"] > 0:
                    await self.run_export()
                    ran = True
                if CFG.get("dms") is not None:
                    await self.run_dm_export()
                    ran = True
                if not ran:
                    log("Nothing to do: pass --guild and/or --dms.")
        except Exception as exc:
            # Log and continue so we still close cleanly and write the log.
            log(f"Export failed: {exc!r}")
            self.failures.append(("export", 0, repr(exc)))
        finally:
            await self._close_http()
            if self.failures:
                log(f"{len(self.failures)} failure(s) during this run:")
                for flabel, fid, ferr in self.failures:
                    log(f"  - {flabel} (ID: {fid}): {ferr}")
            log(f"=== Run end. Elapsed: {elapsed()} ===")
            await self.close()

    # -- HTTP session for downloads ---------------------------------------

    async def _get_http(self) -> aiohttp.ClientSession:
        """Lazily create (and reuse) an aiohttp session for downloads."""
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def _close_http(self) -> None:
        """Shut down the shared aiohttp session, if any."""
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    # -- diagnostics -------------------------------------------------------

    async def dump_channels(self) -> None:
        """--list-channels: print readable channels and/or DM conversations."""
        listed = False
        if CFG.get("dms") is not None:
            await self._dump_dms()
            listed = True
        if CFG["discord_guild_id"] > 0:
            await self._dump_guild_channels()
            listed = True
        if not listed:
            log("Nothing to list: pass --guild and/or --dms.")

    async def _dump_guild_channels(self) -> None:
        """Print each text channel of the selected guild with a readable flag."""
        guild = self.get_guild(CFG["discord_guild_id"]) or \
            await self.fetch_guild(CFG["discord_guild_id"])
        print(f"Guild: {guild.name} ({guild.id})\n")
        viewer = guild.me or self.user
        for channel in sorted(guild.text_channels, key=lambda c: c.position):
            perms = channel.permissions_for(viewer)
            # "ok" = can read message history; "--" = cannot.
            flag = "ok " if (perms.view_channel and perms.read_message_history) else "-- "
            print(f"{flag}[{type(channel).__name__}] #{channel.name} (ID: {channel.id})")

    async def _dump_dms(self) -> None:
        """Print every cached DM and group DM conversation."""
        # Re-fetch the DM list where the library supports it; fall back to
        # whatever's already cached if the call is unavailable or fails.
        try:
            fetch = getattr(self, "fetch_dms", None)
            if fetch is not None:
                await fetch()
        except Exception as exc:
            log(f"Could not fetch DM channels: {exc!r}")
        print("\nDirect messages:")
        for ch in sorted(self.private_channels, key=lambda c: c.id):
            if isinstance(ch, discord.GroupChannel):
                name = ch.name or f"group-{ch.id}"
                n = len(ch.recipients)
                print(f"ok  [group] @{name} (ID: {ch.id}, {n} recipients)")
            elif isinstance(ch, discord.DMChannel):
                rec = getattr(ch, "recipient", None)
                name = rec.name if rec else "(unknown)"
                print(f"ok  [dm] @{name} (ID: {ch.id})")

    # -- channel selection -------------------------------------------------

    async def select_containers(self, guild: discord.Guild) -> List[Any]:
        """
        Build the list of channels (and optionally threads) to archive.

        Selection rules:
          - Filter by channel ID or (substring) name unless no filter given.
          - Skip channels the logged-in user cannot view/read.
          - Optionally append active + archived threads for each selected channel.
        """
        wanted_ids, wanted_names = parse_channel_filter(CFG["discord_channels"])
        strict = CFG["strict_match"]

        def name_ok(name: str) -> bool:
            """True if the channel name matches the filter."""
            lowered = name.lower()
            if lowered in wanted_names:
                return True
            if strict:
                return False
            return any(w in lowered for w in wanted_names)

        def channel_ok(ch: Any) -> bool:
            """True if the channel should be included given the filter."""
            if wanted_ids and ch.id in wanted_ids:
                return True
            if wanted_names and name_ok(getattr(ch, "name", "") or ""):
                return True
            # No filter -> include everything.
            return not wanted_ids and not wanted_names

        viewer = guild.me or self.user
        selected: List[Any] = []
        for channel in guild.text_channels:
            perms = channel.permissions_for(viewer)
            if not perms.read_message_history or not perms.view_channel:
                continue
            if channel_ok(channel):
                selected.append(channel)

        # Threads are appended after all channels so the channels are processed
        # first (nice for logs; not required for correctness). Off by default.
        if CFG["include_threads"]:
            for channel in list(selected):
                try:
                    for thread in channel.threads:
                        selected.append(thread)
                except Exception:
                    # Permission/HTTP errors on the thread list are non-fatal.
                    pass
                try:
                    async for thread in channel.archived_threads(limit=None):
                        selected.append(thread)
                except Exception:
                    pass
        return selected

    async def select_dm_containers(self) -> List[Any]:
        """
        Build the list of DM/group-DM channels to archive.

        Refreshes the DM list via fetch_dms() when available, then filters by
        the optional --dms user-ID set. Empty filter means "every conversation".
        """
        try:
            fetch = getattr(self, "fetch_dms", None)
            if fetch is not None:
                await fetch()
        except Exception as exc:
            log(f"Could not fetch DM channels: {exc!r}")

        wanted = parse_dm_filter(CFG.get("dms") or "")
        selected: List[Any] = []
        for ch in self.private_channels:
            if isinstance(ch, discord.GroupChannel):
                if not wanted or any(u.id in wanted for u in ch.recipients):
                    selected.append(ch)
            elif isinstance(ch, discord.DMChannel):
                rec = getattr(ch, "recipient", None)
                if rec is None:
                    continue
                if not wanted or rec.id in wanted:
                    selected.append(ch)
        # Stable order so logs and manifests are reproducible across runs.
        selected.sort(key=lambda c: c.id)
        return selected

    # -- checkpoint --------------------------------------------------------

    def load_checkpoint(self) -> Dict[str, Any]:
        """Read checkpoint.json (container-id -> resume state). Empty on failure."""
        if not CHECKPOINT_PATH.exists():
            return {}
        try:
            data = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            log("Warning: checkpoint unreadable, starting fresh.")
            return {}

    def save_checkpoint(self, checkpoint: Dict[str, Any]) -> None:
        """Atomically write checkpoint.json (tmp file + replace)."""
        tmp = CHECKPOINT_PATH.with_suffix(".tmp")
        write_text_lf(tmp, json.dumps(checkpoint, indent=2))
        tmp.replace(CHECKPOINT_PATH)

    # -- iteration ---------------------------------------------------------

    async def iter_messages(
        self,
        container: Any,
        state: Dict[str, Optional[int]],
        after_dt: Optional[datetime] = None,
        before_dt: Optional[datetime] = None,
        after_id: Optional[int] = None,
        before_id: Optional[int] = None,
    ) -> AsyncIterator[discord.Message]:
        """Yield messages from a channel/thread/DM with all configured bounds applied."""
        kwargs: Dict[str, Any] = {
            "oldest_first": CFG["oldest_first"],
            "limit": CFG["max_messages_per_channel"],
        }
        after, before = compute_bounds(state, after_id, after_dt, before_id, before_dt)
        if after is not None:
            kwargs["after"] = discord.Object(id=after) if isinstance(after, int) else after
        if before is not None:
            kwargs["before"] = discord.Object(id=before) if isinstance(before, int) else before

        async for message in container.history(**kwargs):
            yield message

    # -- member / role resolution -----------------------------------------

    async def _resolve_member(
        self, guild: discord.Guild, user: Any
    ) -> Optional[discord.Member]:
        """
        Return the guild Member for a user, using cache -> local lookup -> API.
        Caches negative results too, so we don't repeatedly fetch absent users.
        """
        cache_key = (guild.id, user.id)
        if cache_key in self._member_cache:
            return self._member_cache[cache_key]

        member: Optional[discord.Member] = None
        # If the user object is already a Member of this guild, use it directly.
        if isinstance(user, discord.Member) and getattr(user, "guild", None) is guild:
            member = user
        else:
            member = guild.get_member(user.id)
            if member is None:
                try:
                    member = await guild.fetch_member(user.id)
                except discord.NotFound:
                    # User left the guild or never joined.
                    member = None
                except discord.HTTPException as exc:
                    log(f"    (member fetch failed for {user.id}: {exc})")
                    member = None

        self._member_cache[cache_key] = member
        return member

    async def role_names(self, guild: discord.Guild, message: discord.Message) -> str:
        """Return a comma-separated role list for the message author (highest first)."""
        try:
            member = await self._resolve_member(guild, message.author)
        except Exception as exc:
            log(f"    (role lookup failed for {message.author.id}: {exc!r})")
            return ""
        if member is None:
            return ""
        seen: set = set()
        names: List[str] = []
        # reversed(member.roles) yields top-down since roles are low->high internally.
        for role in reversed(member.roles):
            if role.name == "@everyone" or role.name in seen:
                continue
            seen.add(role.name)
            names.append(role.name)
        return ", ".join(names)

    # -- attachments -------------------------------------------------------

    async def download_attachment(
        self, message: discord.Message, attachment: Any, target_dir: Path,
        used_names: Optional[Dict[str, int]] = None,
    ) -> Tuple[str, int]:
        """Return (status, bytes_written); status in {'ok','exists','skipped_size','error'}."""
        # Prefix with message id so filenames are unique within a container.
        name = safe_attachment_name(getattr(attachment, "filename", "attachment"))
        if used_names is not None:
            n = used_names.get(name, 0)
            used_names[name] = n + 1
            if n:
                # Same filename twice on one message: keep both, deterministically.
                name = f"{n + 1}_{name}"
        target = target_dir / f"{message.id}_{name}"
        if target.exists():
            return "exists", 0

        size = int(getattr(attachment, "size", 0) or 0)
        max_bytes = CFG.get("attachment_max_bytes") or 0
        if max_bytes and size > max_bytes:
            return "skipped_size", 0

        url = getattr(attachment, "url", None)
        if not url:
            return "error", 0

        timeout = float(CFG.get("attachment_timeout_seconds") or 30.0)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            session = await self._get_http()
            # Download to a .tmp file, then atomically rename on success.
            tmp = target.with_name(target.name + ".tmp")
            written = 0
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return "error", 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
                        written += len(chunk)
            # os.replace is atomic on NTFS and POSIX. On Windows, a locked
            # target (e.g. an AV scanner) makes this raise; caller logs it.
            tmp.replace(target)
            return "ok", written
        except Exception as exc:
            log(f"    attachment download failed for {message.id}/{name}: {exc!r}")
            # Clean up the partial file so a retry sees a clean slate.
            try:
                tmp_path = target.with_name(target.name + ".tmp")
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return "error", 0

    # -- row building ------------------------------------------------------

    async def build_row(
        self,
        guild: Optional[discord.Guild],
        container: Any,
        message: discord.Message,
    ) -> Dict[str, Any]:
        """Convert a discord.Message into a flat dict matching FIELDNAMES."""
        is_thread = isinstance(container, discord.Thread)
        is_group = isinstance(container, discord.GroupChannel)
        is_dm = isinstance(container, discord.DMChannel) or is_group

        # Resolve the 'channel' column and 'thread' column for every container
        # shape. Threads store the parent channel's name in 'channel'.
        if is_thread:
            parent = container.parent
            channel_name = getattr(parent, "name", "") or ""
            thread_name = container.name or ""
        elif is_group:
            channel_name = container.name or f"group-{container.id}"
            thread_name = ""
        elif is_dm:
            rec = getattr(container, "recipient", None)
            channel_name = (rec.name if rec else None) or f"dm-{container.id}"
            thread_name = ""
        else:
            channel_name = getattr(container, "name", "") or ""
            thread_name = ""

        author = message.author
        author_username = getattr(author, "name", None) or ""
        author_display_name = getattr(author, "display_name", None) or author_username
        author_id = getattr(author, "id", 0)
        author_is_bot = bool(getattr(author, "bot", False))

        # Reply target, if the message is a reply.
        reply_to = ""
        if message.reference and message.reference.message_id:
            reply_to = str(message.reference.message_id)

        # Discord timestamps are UTC already; isoformat keeps them sortable.
        edited = message.edited_at.isoformat() if message.edited_at else ""

        message_type = _message_type_name(message)
        content = message.content or ""
        # System messages often have empty content; synthesize a description.
        if _is_system_message(message) and not content:
            try:
                system_text = getattr(message, "system_content", "") or ""
            except Exception:
                system_text = ""
            content = system_text or _describe_system_message(message)

        # Role lookup is expensive (one member fetch per unique author) and
        # is meaningless in DMs (no guild roles). Skip it for DM containers.
        roles = ""
        if not is_dm and CFG.get("include_roles") and guild is not None:
            roles = await self.role_names(guild, message)

        reactions = _format_reaction_counts(message) if CFG.get("include_reactions") else ""
        reaction_users = ""
        if CFG.get("include_reaction_users"):
            # Expensive: one request per (message, emoji). Only do this if asked.
            reaction_users = await _format_reaction_users(message)

        return {
            "channel": channel_name,
            "thread": thread_name,
            "channel_id": container.id,
            "message_id": message.id,
            "message_type": message_type,
            "author_username": author_username,
            "author_display_name": author_display_name,
            "author_id": author_id,
            "author_is_bot": author_is_bot,
            "timestamp_utc": message.created_at.isoformat(),
            "edited_timestamp_utc": edited,
            "content": content,
            "attachment_urls": " | ".join(a.url for a in message.attachments),
            "reply_to_message_id": reply_to,
            "roles": roles,
            "reactions": reactions,
            "reaction_users": reaction_users,
        }

    # -- path helpers ------------------------------------------------------

    def _container_label(self, container: Any) -> str:
        """Short label used in logs: '#channel', '~thread', or '@dm'."""
        if isinstance(container, discord.GroupChannel):
            name = container.name or f"group-{container.id}"
            return f"@{name}"
        if isinstance(container, discord.DMChannel):
            rec = getattr(container, "recipient", None)
            return f"@{rec.name}" if rec else f"@{container.id}"
        prefix = "~" if isinstance(container, discord.Thread) else "#"
        return f"{prefix}{container.name}"

    def _container_dir(self, guild_dir: Path, container: Any) -> Path:
        """Resolve the on-disk directory for a channel, thread, or DM."""
        if isinstance(container, discord.GroupChannel):
            name = container.name or "group"
            return guild_dir / f"group__{slugify(name)}__{container.id}"
        if isinstance(container, discord.DMChannel):
            rec = getattr(container, "recipient", None)
            name = rec.name if rec else "dm"
            return guild_dir / f"dm__{slugify(name)}__{container.id}"
        if isinstance(container, discord.Thread):
            parent = container.parent
            if parent is None:
                # Orphaned thread (parent deleted) — put it directly under guild.
                return guild_dir / folder_name(container.name, container.id)
            parent_dir = guild_dir / folder_name(parent.name, parent.id)
            return parent_dir / "threads" / folder_name(container.name, container.id)
        return guild_dir / folder_name(container.name, container.id)

    # -- per-container archiving ------------------------------------------

    async def _archive_container_safe(self, *args: Any) -> Optional[Dict[str, int]]:
        """
        Run _archive_container, but let one bad channel (403, deleted mid-run,
        network error) fail alone instead of aborting the whole export.
        Returns None on failure; the error is recorded in self.failures.
        """
        container = args[4]
        try:
            return await self._archive_container(*args)
        except Exception as exc:
            label = self._container_label(container)
            log(f"    !! {label} failed: {exc!r} -- continuing with the next one")
            self.failures.append((label, container.id, repr(exc)))
            return None

    async def _archive_container(
        self,
        guild: Optional[discord.Guild],
        guild_id: int,
        guild_name: str,
        root_dir: Path,
        container: Any,
        checkpoint: Dict[str, Any],
        after_dt: Optional[datetime],
        before_dt: Optional[datetime],
        after_id: Optional[int],
        before_id: Optional[int],
    ) -> Dict[str, int]:
        """
        Archive one channel/thread/DM into root_dir.

        Shared by guild mode and DM mode. Returns per-container counters:
        {seen, count, skipped, att_ok, att_skip, att_err}.
        """
        key = str(container.id)
        label = self._container_label(container)
        container_dir = self._container_dir(root_dir, container)
        messages_dir = container_dir / "messages"
        attachments_dir = container_dir / "attachments"

        # Windows long-path check (no-op elsewhere)
        warn_long_path(messages_dir / "part-001.jsonl")

        # Log a path relative to the output dir; absolute is too noisy.
        try:
            rel = container_dir.relative_to(OUT_DIR)
        except ValueError:
            rel = container_dir
        log(f"--- {label} (ID: {container.id}) -> {rel}")

        writer = ContainerWriter(
            messages_dir,
            CFG["max_messages_per_file"],
            CFG["write_csv"],
            FIELDNAMES,
        )

        # Per-container counters reset for each iteration.
        seen = count = skipped = att_ok = att_skip = att_err = 0

        # Resume state for this container (reads legacy checkpoint formats too).
        state = load_scan_state(checkpoint.get(key))
        min_seen: Optional[int] = None
        max_seen: Optional[int] = None
        complete = False                       # True only if the pass finishes
        checkpoint_every = CFG["messages_per_batch"]

        def snapshot(is_complete: bool) -> None:
            """Fold this pass's progress into the in-memory checkpoint."""
            new_state = serialize_scan_state(next_scan_state(
                state, CFG["oldest_first"], min_seen, max_seen, is_complete))
            if new_state:
                checkpoint[key] = new_state
            else:
                checkpoint.pop(key, None)

        try:
            async for message in self.iter_messages(
                container, state, after_dt, before_dt, after_id, before_id,
            ):
                seen += 1
                if max_seen is None or message.id > max_seen:
                    max_seen = message.id
                if min_seen is None or message.id < min_seen:
                    min_seen = message.id
                is_system = _is_system_message(message)

                if CFG["skip_system_messages"] and is_system:
                    # Skip the row, but still advance the checkpoint below.
                    skipped += 1
                else:
                    row = await self.build_row(guild, container, message)
                    writer.write(row)
                    count += 1

                if CFG["download_attachments"] and message.attachments:
                    used_names: Dict[str, int] = {}
                    for att in message.attachments:
                        status, _b = await self.download_attachment(
                            message, att, attachments_dir, used_names
                        )
                        if status == "ok":
                            att_ok += 1
                        elif status in ("exists", "skipped_size"):
                            att_skip += 1
                        else:
                            att_err += 1

                # Periodic progress logging.
                every = CFG["progress_every_n_messages"]
                if every and seen % every == 0:
                    log(
                        f"    {label}: {seen} fetched, {count} written, "
                        f"{skipped} skipped, attachments +{att_ok}"
                    )

                # Batch pacing (helps avoid rate limits on large channels).
                if (
                    CFG["respect_rate_limits"]
                    and CFG["messages_per_batch"]
                    and seen % CFG["messages_per_batch"] == 0
                ):
                    await asyncio.sleep(CFG["pause_after_batch_seconds"])

                # Durable progress: flush rows first, then record the
                # checkpoint, so a hard kill never leaves the checkpoint
                # ahead of what is on disk.
                if checkpoint_every and seen % checkpoint_every == 0:
                    writer.flush()
                    snapshot(False)
                    try:
                        self.save_checkpoint(checkpoint)
                    except OSError as exc:
                        log(f"    (checkpoint save failed: {exc!r})")

            complete = True
            writer.flush()
        finally:
            # Always close the writer and persist the checkpoint, even on
            # cancellation/error, so the next run resumes correctly.
            writer.close()
            snapshot(complete)
            try:
                self.save_checkpoint(checkpoint)
            except Exception as exc:
                log(f"    (checkpoint save failed: {exc!r})")

        # Determine kind + parent pointer + a human-readable container name
        # for the manifest. Threads point back to their parent channel.
        parent_id = parent_name = None
        if isinstance(container, discord.Thread):
            kind = "thread"
            p = container.parent
            if p is not None:
                parent_id = p.id
                parent_name = p.name
            container_name = getattr(container, "name", "") or ""
        elif isinstance(container, discord.GroupChannel):
            kind = "group"
            container_name = container.name or f"group-{container.id}"
        elif isinstance(container, discord.DMChannel):
            kind = "dm"
            rec = getattr(container, "recipient", None)
            container_name = (rec.name if rec else None) or f"dm-{container.id}"
        else:
            kind = "channel"
            container_name = getattr(container, "name", "") or ""

        manifest = write_container_manifest(
            container_dir,
            kind,
            container.id,
            container_name,
            guild_id,
            guild_name,
            parent_id=parent_id,
            parent_name=parent_name,
        )

        if count == 0 and skipped == 0:
            log(f"    {label}: nothing new (parts on disk: {manifest['total_parts']})")
        else:
            log(
                f"    {label}: {count} written, {skipped} skipped, "
                f"attachments +{att_ok} (skip {att_skip}, err {att_err}), "
                f"parts on disk: {manifest['total_parts']}"
            )

        return {
            "seen": seen,
            "count": count,
            "skipped": skipped,
            "att_ok": att_ok,
            "att_skip": att_skip,
            "att_err": att_err,
        }

    # -- manifests / index -------------------------------------------------

    def _write_root_manifest(
        self,
        root_dir: Path,
        kind: str,
        root_id: int,
        root_name: str,
    ) -> None:
        """
        Aggregate per-container manifests into a root-level _manifest.json.

        Called for a guild folder (kind='guild') and for the DM root folder
        (kind='dms'). Both use the same on-disk shape: a directory whose
        children are container folders (channels, DMs, or group DMs).
        """
        channels: List[dict] = []
        threads = 0
        total_messages = 0
        total_parts = 0
        total_attachments = 0

        # First pass: read each child manifest and collect totals.
        for ch_dir in sorted(root_dir.iterdir()):
            if not ch_dir.is_dir() or ch_dir.name.startswith("_"):
                continue
            ch_manifest = ch_dir / "_manifest.json"
            if not ch_manifest.exists():
                continue
            try:
                data = json.loads(ch_manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            channels.append({
                "id": data.get("id"),
                "name": data.get("name"),
                "folder": ch_dir.name,
                "kind": data.get("kind", "channel"),
                "total_messages": data.get("total_messages", 0),
                "total_parts": data.get("total_parts", 0),
                "attachment_count": data.get("attachment_count", 0),
                "thread_count": data.get("thread_count", 0),
            })
            total_messages += int(data.get("total_messages", 0) or 0)
            total_parts += int(data.get("total_parts", 0) or 0)
            total_attachments += int(data.get("attachment_count", 0) or 0)
            threads += int(data.get("thread_count", 0) or 0)

            # Threads are stored under <channel>/threads/<thread>/ — walk them
            # too so root-level totals are complete.
            threads_dir = ch_dir / "threads"
            if threads_dir.exists():
                for th_dir in threads_dir.iterdir():
                    if not th_dir.is_dir():
                        continue
                    th_manifest = th_dir / "_manifest.json"
                    if not th_manifest.exists():
                        continue
                    try:
                        tdata = json.loads(th_manifest.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    total_messages += int(tdata.get("total_messages", 0) or 0)
                    total_parts += int(tdata.get("total_parts", 0) or 0)
                    total_attachments += int(tdata.get("attachment_count", 0) or 0)

        manifest = {
            "kind": kind,
            "id": root_id,
            "name": root_name,
            "folder": root_dir.name,
            "channel_count": len(channels),
            "thread_count": threads,
            "total_messages": total_messages,
            "total_parts": total_parts,
            "total_attachments": total_attachments,
            "channels": channels,
            "scan_order": "oldest_first" if CFG.get("oldest_first") else "newest_first",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_text_lf(
            root_dir / "_manifest.json",
            json.dumps(manifest, indent=2, ensure_ascii=False),
        )

    def _write_index(self) -> None:
        """Write the top-level _index.json listing every root folder."""
        roots: List[dict] = []
        for g_dir in sorted(OUT_DIR.iterdir()):
            # Skip files; only directories are roots. (Internal directories,
            # if any, are listed in _INTERNAL_DIRS below and skipped.)
            if not g_dir.is_dir():
                continue
            manifest_path = g_dir / "_manifest.json"
            if not manifest_path.exists():
                continue
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            roots.append({
                "kind": data.get("kind", "guild"),
                "id": data.get("id"),
                "name": data.get("name"),
                "folder": g_dir.name,
                "channel_count": data.get("channel_count", 0),
                "thread_count": data.get("thread_count", 0),
                "total_messages": data.get("total_messages", 0),
                "total_attachments": data.get("total_attachments", 0),
            })

        index = {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "out_dir": str(OUT_DIR),
            "scan_order": "oldest_first" if CFG.get("oldest_first") else "newest_first",
            "written_formats": ["jsonl"] + (["csv"] if CFG.get("write_csv") else []),
            "roots": roots,
        }
        write_text_lf(
            OUT_DIR / "_index.json",
            json.dumps(index, indent=2, ensure_ascii=False),
        )

    # -- main export (guild mode) -----------------------------------------

    async def run_export(self) -> None:
        """Guild export loop: for each selected container, iterate and write."""
        guild = self.get_guild(CFG["discord_guild_id"]) or \
            await self.fetch_guild(CFG["discord_guild_id"])
        log(f"Guild: {guild.name} ({guild.id})")
        log(f"Scan order: {'oldest -> newest' if CFG['oldest_first'] else 'newest -> oldest'}")
        log(f"Write CSV: {CFG['write_csv']}   Attachments: {CFG['download_attachments']}")
        log(f"Threads: {CFG['include_threads']}   Roles: {CFG['include_roles']}")
        log(f"Reactions: counts={CFG['include_reactions']} "
            f"users={CFG['include_reaction_users']}")

        # Every run writes under <out_dir>/<guild-folder>/.
        guild_dir = OUT_DIR / folder_name(guild.name, guild.id)
        guild_dir.mkdir(parents=True, exist_ok=True)

        containers = await self.select_containers(guild)
        log(f"Channels selected: {len(containers)}")
        if not containers:
            log("Nothing matched. Check --channels.")
            self._write_root_manifest(guild_dir, "guild", guild.id, guild.name)
            self._write_index()
            return
        for c in containers:
            log(f"  - {self._container_label(c)} (ID: {c.id})")

        # Pre-parse range args once (validate_config already sanity-checked them).
        after_dt = parse_date_arg(CFG.get("date_after"))
        before_dt = parse_date_arg(CFG.get("date_before"))
        after_id = CFG.get("message_id_after")
        before_id = CFG.get("message_id_before")
        if any(v is not None for v in (after_dt, before_dt, after_id, before_id)):
            log(
                f"Range: after="
                f"{after_id if after_id is not None else after_dt or '(beginning)'} "
                f"before="
                f"{before_id if before_id is not None else before_dt or '(now)'}"
            )

        # checkpoint maps container-id -> last successfully archived message-id.
        checkpoint = self.load_checkpoint()

        # Grand totals across all containers, reported at the end.
        grand = {"count": 0, "skipped": 0,
                 "att_ok": 0, "att_skip": 0, "att_err": 0}

        for container in containers:
            r = await self._archive_container_safe(
                guild, guild.id, guild.name, guild_dir, container,
                checkpoint, after_dt, before_dt, after_id, before_id,
            )
            if r is None:
                continue
            for k in grand:
                grand[k] += r[k]

        # Final aggregate artifacts.
        self._write_root_manifest(guild_dir, "guild", guild.id, guild.name)
        self._write_index()

        log(
            f"Done. {grand['count']} message(s) written across {len(containers)} container(s)."
        )
        if grand["skipped"]:
            log(f"     ({grand['skipped']} system message(s) skipped)")
        if CFG["download_attachments"]:
            log(
                f"Attachments: {grand['att_ok']} downloaded, "
                f"{grand['att_skip']} skipped, {grand['att_err']} errors."
            )

    # -- main export (DM mode) --------------------------------------------

    async def run_dm_export(self) -> None:
        """DM export loop: for each selected conversation, iterate and write."""
        log("DM mode enabled.")
        log(f"Scan order: {'oldest -> newest' if CFG['oldest_first'] else 'newest -> oldest'}")
        log(f"Write CSV: {CFG['write_csv']}   Attachments: {CFG['download_attachments']}")
        log(f"Roles: skipped (no guild roles in DMs)")
        log(f"Reactions: counts={CFG['include_reactions']} "
            f"users={CFG['include_reaction_users']}")

        # DM root folder is keyed by *our* user id, not a guild id.
        dm_root = OUT_DIR / f"_dms__{self.user.id}"
        dm_root.mkdir(parents=True, exist_ok=True)

        containers = await self.select_dm_containers()
        log(f"DM conversations selected: {len(containers)}")
        if not containers:
            log("No DM conversations matched.")
            self._write_root_manifest(dm_root, "dms", self.user.id, "Direct Messages")
            self._write_index()
            return
        for c in containers:
            log(f"  - {self._container_label(c)} (ID: {c.id})")

        # Pre-parse range args once (validate_config already sanity-checked them).
        after_dt = parse_date_arg(CFG.get("date_after"))
        before_dt = parse_date_arg(CFG.get("date_before"))
        after_id = CFG.get("message_id_after")
        before_id = CFG.get("message_id_before")
        if any(v is not None for v in (after_dt, before_dt, after_id, before_id)):
            log(
                f"Range: after="
                f"{after_id if after_id is not None else after_dt or '(beginning)'} "
                f"before="
                f"{before_id if before_id is not None else before_dt or '(now)'}"
            )

        checkpoint = self.load_checkpoint()
        grand = {"count": 0, "skipped": 0,
                 "att_ok": 0, "att_skip": 0, "att_err": 0}

        for container in containers:
            r = await self._archive_container_safe(
                None, 0, "Direct Messages", dm_root, container,
                checkpoint, after_dt, before_dt, after_id, before_id,
            )
            if r is None:
                continue
            for k in grand:
                grand[k] += r[k]

        self._write_root_manifest(dm_root, "dms", self.user.id, "Direct Messages")
        self._write_index()

        log(
            f"Done. {grand['count']} DM message(s) written across "
            f"{len(containers)} conversation(s)."
        )
        if grand["skipped"]:
            log(f"     ({grand['skipped']} system message(s) skipped)")
        if CFG["download_attachments"]:
            log(
                f"Attachments: {grand['att_ok']} downloaded, "
                f"{grand['att_skip']} skipped, {grand['att_err']} errors."
            )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def redact(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of cfg for display. Currently a no-op passthrough."""
    return dict(cfg)


def main() -> None:
    """Parse args, validate, prepare output dir, and run the client."""
    global CFG, TOKEN, OUT_DIR, CHECKPOINT_PATH, LOG_PATH, LIST_CHANNELS

    parser = build_parser()

    # Normalize -help to --help (argparse only knows -h and --help), then
    # route targeted help (e.g. "--guild -h") before argparse runs. Otherwise
    # argparse would try to consume -h as the value of the preceding option
    # and error out.
    argv = ["--help" if a == "-help" else a for a in sys.argv[1:]]
    _handle_targeted_help(parser, argv)

    args = parser.parse_args(argv)

    CFG = resolve_config(args)
    validate_config(CFG)

    # --show-config is a dry run: no token needed, no files touched.
    if args.show_config:
        print(json.dumps(redact(CFG), indent=2))
        return

    # Offer to create a template .env on first run (may exit).
    ensure_env_file()
    load_env()

    TOKEN = os.environ.get("DISCORD_TOKEN", "").strip()
    if not TOKEN:
        if sys.platform == "win32":
            perm_hint = (
                "On Windows, restrict the file via Properties -> Security."
            )
        else:
            perm_hint = "Then run: chmod 600 .env"
        sys.exit(
            "No token found.\n"
            "Create a .env file next to this script containing:\n"
            "    DISCORD_TOKEN=your-token-here\n"
            f"{perm_hint}"
        )

    # Set up output paths and per-run log file.
    OUT_DIR = Path(CFG["out_dir"])
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_PATH = OUT_DIR / "checkpoint.json"
    LOG_PATH = OUT_DIR / "_run.log"
    LIST_CHANNELS = args.list_channels

    # In --list-channels mode we don't want to touch the export log/snapshot.
    if not LIST_CHANNELS:
        write_config_snapshot(OUT_DIR, CFG)
        log("=== Run start ===")

    archiver = Archiver()
    try:
        archiver.run(TOKEN)
    except discord.LoginFailure:
        sys.exit("Login failed: Discord rejected the token. Check DISCORD_TOKEN.")
    except KeyboardInterrupt:
        log(f"Interrupted. Elapsed: {elapsed()}")
    # Non-zero exit when any container or the export itself failed, so cron
    # jobs and wrapper scripts can notice.
    if archiver.failures:
        sys.exit(1)


if __name__ == "__main__":
    main()