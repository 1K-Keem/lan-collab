"""Terminal workspace with slash completion and a native tkinter file picker."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Thread
from time import monotonic
import os
from queue import Empty, Queue
import re
import sqlite3
from unicodedata import combining, east_asian_width

from lancollab.client.application import ClientState, safe_display
from lancollab.client.cli import Command, help_reference, network_status, parse_command

from lancollab.client.settings import load_theme, save_theme
from lancollab.client.clipboard import ClipboardError, copy_text


@dataclass(frozen=True)
class Suggestion:
    value: str
    usage: str
    description: str
    admin: bool = False


COMMANDS = (
    Suggestion("/reply", "TEXT", "Reply to the last DM peer"),
    Suggestion("/transfers", "", "Active transfers and progress"),
    Suggestion("/files", "", "Your downloaded files"),
    Suggestion("/search", "TEXT", "Search local conversations"),
    Suggestion("/ping", "", "Measure encrypted TCP latency"),
    Suggestion("/whoami", "", "Account and connection details"),
    Suggestion("/clear", "", "Clear the conversation display"),
    Suggestion("/filter", "CATEGORY", "all, global, dm, files or groups"),
    Suggestion("/all", "TEXT", "Message everyone online"),
    Suggestion("/msg", "USER TEXT", "Send a private message"),
    Suggestion("/groups", "", "List your private groups"),
    Suggestion("/group", "ACTION NAME [USER...]", "create, open, add, kick, delete or leave"),
    Suggestion("/gmsg", "NAME TEXT", "Send a group message"),
    Suggestion("/send", "USER PATH", "Offer a file to someone"),
    Suggestion("/sendgroup", "NAME PATH", "Offer a file to online group members"),
    Suggestion("/users", "", "See who is online"),
    Suggestion(
        "/history",
        "[options]",
        "Saved people and sessions",
    ),
    Suggestion("/accept", "[ID] [PATH]", "Accept a file into an optional folder"),
    Suggestion("/reject", "ID", "Decline a file offer"),
    Suggestion("/cancel", "ID", "Cancel a file transfer"),
    Suggestion("/status", "", "Connection and network status"),
    Suggestion("/net", "", "Check network quality"),
    Suggestion("/pending", "", "List accounts awaiting approval", True),
    Suggestion("/approve", "USER", "Approve a new member", True),
    Suggestion("/reject-user", "USER", "Reject a pending account", True),
    Suggestion("/theme", "[dark|light]", "Switch between dark and light colors"),
    Suggestion("/help", "", "Show all commands"),
    Suggestion("/quit", "", "Close the app; keep remembered login"),
    Suggestion("/logout", "", "Log out and forget this device"),
)


def suggestions(text, users=(), role="member", pending=(), transfers=()):
    """Complete commands or the first username argument; never rewrite message text."""
    if text.startswith("@") and " " not in text:
        return [
            Suggestion("@" + name, "TEXT", "Private message")
            for name in sorted(users)
            if name.startswith(text[1:])
        ]
    if not text.startswith("/"):
        return []
    if " " not in text:
        return [
            c
            for c in COMMANDS
            if c.value.startswith(text.lower()) and (not c.admin or role == "admin")
        ]
    command, argument = text.split(" ", 1)
    if " " not in argument:
        if command in {"/approve", "/reject-user"} and role == "admin":
            return [
                Suggestion(f"{command} {name}", "", "Pending account")
                for name in pending
                if name.startswith(argument)
            ]
        if command in {"/accept", "/reject", "/cancel"}:
            return [
                Suggestion(f"{command} {row['id'][:8]}", "", row["name"])
                for row in transfers
                if row["id"].startswith(argument)
                and (command == "/cancel" or row["status"] == "offer")
            ]
        if command == "/theme":
            return [
                Suggestion(f"/theme {name}", "", f"{name.capitalize()} colors")
                for name in ("dark", "light")
                if name.startswith(argument)
            ]
        if command == "/filter":
            return [
                Suggestion(f"/filter {name}", "", "Message category")
                for name in FILTERS
                if name.startswith(argument)
            ]
    if command in {"/msg", "/send", "/history"} and " " not in argument:
        candidates = (
            [("people", "Browse saved people"), ("sessions", "Browse saved sessions")]
            if command == "/history"
            else [(name, "Online user") for name in sorted(users)]
        )
        return [
            Suggestion(f"{command} {name}", "", description)
            for name, description in candidates
            if name.startswith(argument)
        ]
    return []


FILTERS = ("all", "global", "groups", "dm", "files")


def notice_category(message):
    if chat_group(message) is not None:
        return "groups"
    if message.startswith(("[DM]", "[chat] [CHAT_PRIVATE]", "[CHAT_PRIVATE]")):
        return "dm"
    if message.startswith(("[All]", "[chat] [CHAT_GLOBAL]", "[CHAT_GLOBAL]")):
        return "global"
    if message.startswith("[file]"):
        return "files"
    return "other"


def cell_width(text):
    return sum(0 if combining(c) else 2 if east_asian_width(c) in "WF" else 1 for c in text)


def fit_cells(text, width):
    result, used = [], 0
    for char in text:
        size = cell_width(char)
        if used + size > width:
            break
        result.append(char)
        used += size
    return "".join(result)


def wrap_cells(text, width):
    """Preserve spaces and Unicode while wrapping to terminal display columns."""
    rows = []
    while text:
        part = fit_cells(text, width)
        if not part:
            break
        rows.append(part)
        text = text[len(part) :]
    return rows or [""]


def wrap_prefixed(prefix, text, width):
    """Wrap text so the first line leaves room for prefix.

    Returns ``(prefix, line)`` rows; only the first row carries the
    prefix, so a timestamp stays on the first line and never repeats.
    """
    rows = []
    rest = text
    first = max(1, width - cell_width(prefix))
    part = fit_cells(rest, first)
    rows.append((prefix, part))
    rest = rest[len(part) :]
    while rest:
        part = fit_cells(rest, width)
        rows.append(("", part))
        rest = rest[len(part) :]
    return rows


def chat_sender(message):
    """Sender of a chat notice, or None if the line is not a chat message.

    Notices carry a ``[chat] `` prefix before the ``[CHAT_*]`` part.
    """
    match = re.match(r"^(?:\[chat\] )?\[(?:All|DM|CHAT_GLOBAL|CHAT_PRIVATE|#[A-Za-z0-9_.-]{1,32})\] ([^:]+):", message)
    return match.group(1) if match else None

def chat_group(message):
    match = re.match(r"^(?:\[chat\] )?\[#([A-Za-z0-9_.-]{1,32})\] ", message)
    return match.group(1) if match else None


_USERNAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")


def valid_username(name):
    """Only names the command parser can address unambiguously become clickable."""
    return isinstance(name, str) and _USERNAME.fullmatch(name) is not None


def choose_file(*, folder=False):
    """Native file/folder dialog; an empty result means cancellation."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        raise RuntimeError(
            "Picker needs tkinter; use /accept ID or /send USER PATH instead."
        ) from error
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if folder:
            return filedialog.askdirectory(
                parent=root, title="Choose where to save the file", mustexist=True
            )
        return filedialog.askopenfilename(parent=root, title="Choose a file to send")
    except tk.TclError as error:
        raise RuntimeError(
            "Cannot open picker; use /accept ID or /send USER PATH instead."
        ) from error
    finally:
        if root is not None:
            root.destroy()


def composer_guidance(text):
    """Hint for a bare /msg or /send template, including path guidance."""
    head, _, rest = text.partition(" ")
    argument = rest.strip()
    if argument and " " not in argument:
        if head in {"/send", "/sendgroup"}:
            return "File: type or paste a path (quote spaces), then Enter"
        if head == "/msg":
            return "Message: type your private message, then Enter"
    return None


SENDER_STYLES = ("sender_a", "sender_b", "sender_c", "sender_d", "sender_e")


def sender_style_name(sender, username):
    """Own messages use the accent; others get a stable color per sender."""
    if sender is None:
        return None
    if sender in {"you", username} or sender.startswith("you ->"):
        return "accent"
    return SENDER_STYLES[sum(sender.encode("utf-8")) % len(SENDER_STYLES)]


def display_time(timestamp):
    """Local date and minute; omit protocol-style seconds and timezone offsets."""
    return datetime.fromisoformat(timestamp).astimezone().strftime("%d %b %Y · %H:%M")


def transfer_summary(row):
    status = row["status"]
    if status == "offer":
        return "Accept or decline"
    if status == "waiting":
        return "Waiting for acceptance"
    if status == "verifying":
        return "Checking file…"
    percent = min(100, int(100 * row["done"] / row["size"])) if row["size"] else 0
    return f"{'Receiving' if status == 'receiving' else 'Sending'} · {percent}%"


def transfer_bar(row, width):
    """Thin progress bar for a running transfer, or None while it waits."""
    if row["status"] not in {"sending", "receiving"} or width < 8:
        return None
    fraction = min(1, row["done"] / row["size"]) if row["size"] else 0
    room = width - 5
    filled = int(fraction * room)
    return [("━" * filled, "file"), ("─" * (room - filled), "faint"), (f" {fraction:4.0%}", "muted")]


# A rendered row is a list of (text, style) segments drawn left to right.


def row_text(row):
    return "".join(text for text, _ in row)


def fit_row(row, width):
    """Clip a segment row to ``width`` display cells."""
    result, used = [], 0
    for text, style in row:
        part = fit_cells(text, max(0, width - used))
        if part:
            result.append((part, style))
            used += cell_width(part)
        if len(part) < len(text):
            break
    return result


def spread_row(left, right, width):
    """Left segments, then right segments flush with the right edge if they fit."""
    gap = width - cell_width(row_text(left)) - cell_width(row_text(right))
    if not right or gap < 2:
        return fit_row(left, width)
    return left + [(" " * gap, "")] + right


def wrap_words(text, width):
    """Wrap at spaces where possible; only words wider than a row are split."""
    width = max(1, width)
    rows, line = [], ""
    for word in re.findall(r"\S+\s*|\s+", text):
        if cell_width(line + word.rstrip()) <= width:
            line += word
            continue
        if line.strip():
            rows.append(line.rstrip())
        line = ""
        while cell_width(word.rstrip()) > width:
            part = fit_cells(word, width) or word[0]  # A wide glyph in a 1-cell row.
            rows.append(part)
            word = word[len(part) :]
        line += word
    rows.append(fit_cells(line.rstrip(), width))
    return rows


def message_rows(timestamp, sender, text, channel, width, username):
    """Chat card: a colored sender line, then the body indented beneath it.

    Identity survives without color: the bullet marks others, the chevron
    marks your own messages, and a blank row closes every card.
    """
    width = max(1, width)
    sender, text = safe_display(sender), safe_display(text)
    own = sender in {"you", username} or sender.startswith("you ->")
    label = "You" + sender[3:] if sender.startswith("you") and own else sender
    style = sender_style_name(sender, username) or "muted"
    meta = " · ".join(part for part in (channel, timestamp) if part)
    rows = [fit_row([("› " if own else "● ", style), (label, style), ("  " + meta, "muted")], width)]
    indent = "  " if width > 4 else ""
    for part in wrap_words(text, width - len(indent)):
        rows.append([(indent, ""), (part, "text")])
    rows.append([])
    return rows


NOTICE_GLYPHS = {"error": "✗", "file": "↕", "admin": "◆", "success": "✓", "dm": "•"}


def notice_rows(stamp, text, kind, width):
    """System line: glyph, wrapped text, and the time at the right edge."""
    width = max(1, width)
    glyph = NOTICE_GLYPHS.get(kind, "•")
    body = "muted" if kind == "muted" else kind
    lines = wrap_words(text, max(1, width - 2))
    rows = [[(glyph + " ", "faint" if kind == "muted" else kind), (lines[0], body)]]
    if stamp:
        rows[0] = spread_row(rows[0], [(stamp, "faint")], width)
    rows += [[("  ", ""), (line, body)] for line in lines[1:]]
    return [fit_row(row, width) for row in rows]


def welcome_rows(width, username):
    """Opening card in the All tab; redrawn at the current width."""
    lines = [
        [("✻ ", "accent"), ("Welcome to LAN Collab", "title")]
        + ([(", " + safe_display(username), "title")] if username else []),
        [],
        [("Type a message to chat with everyone online.", "muted")],
        [("/ ", "key"), ("commands  ", "muted"), ("@name ", "key"), ("private chat  ", "muted"),
         ("F1 ", "key"), ("help", "muted")],
        [("F7 ", "key"), ("people  ", "muted"), ("F8 ", "key"), ("history  ", "muted"),
         ("Alt+1…5 ", "key"), ("tabs", "muted")],
    ]
    inner = min(width, 66) - 4
    if inner < 16:
        return [fit_row(line, width) for line in lines] + [[]]
    rows = [[("╭" + "─" * (inner + 2) + "╮", "faint")]]
    for line in lines:
        line = fit_row(line, inner)
        pad = inner - cell_width(row_text(line))
        rows.append([("│ ", "faint")] + line + [(" " * pad + " │", "faint")])
    rows.append([("╰" + "─" * (inner + 2) + "╯", "faint")])
    rows.append([])
    return rows


@dataclass(frozen=True)
class CopyMessage:
    label: str
    text: str


def transcript_copy_messages(rows):
    return [
        CopyMessage(
            f"{display_time(timestamp)} · {'you' if direction == 'sent' else peer}"
            + (f" → {peer}" if direction == "sent" and kind == "CHAT_PRIVATE" else ""),
            text,
        )
        for timestamp, direction, kind, peer, text in rows
    ]


class CopyPicker:
    """A snapshot: incoming messages cannot change what the user is copying."""

    def __init__(self, messages, selected=None):
        self.messages = tuple(messages)
        self.selected = max(
            0, min(len(messages) - 1, len(messages) - 1 if selected is None else selected)
        )
        self.scroll = 0
        self.notice = None

    def move(self, step):
        self.selected = max(0, min(len(self.messages) - 1, self.selected + step))
        self.scroll = 0
        self.notice = None

    def copy(self):
        if not self.messages:
            return
        try:
            copy_text(self.messages[self.selected].text)
            self.notice = ("Copied to clipboard.", "success")
        except ClipboardError as error:
            self.notice = (str(error), "error")


class HistoryBrowser:
    """Full-screen sessions and person transcripts with confirmed local deletion."""

    def __init__(self, session, args=()):
        self.session = session
        self.people = session.history_people()
        self.records = session.history_sessions()
        self.level = "sessions"
        self.peer = None
        self.transcript = []
        self.session_ids = []
        self.record = None
        self.selected = self.scroll = 0
        self.parents = []
        self.error = None
        self._rows = []
        self._width = None
        self._open_at_end = False
        self.pending_delete = None
        self.notice = None
        if args == ("people",):
            self.level = "people"
        elif args and args[0] == "show" and len(args) >= 2:
            self._open_session(args[1], args[2] if len(args) == 3 else None)
        elif args and args != ("sessions",):
            self.level = "people"
            self._open_person(None if args == ("all",) else args[0])

    @property
    def breadcrumb(self):
        account = safe_display(self.session.client.username or "")
        if self.level == "sessions":
            return f"{account} › Sessions"
        if self.level == "people":
            return f"{account} › People"
        scope = display_time(self.record.started) if self.record else "All sessions"
        if self.peer:
            return f"{account} › {safe_display(self.peer)} › {scope}"
        return f"{account} › {scope}"

    def items(self):
        """List rows for the people or sessions level, as segment rows."""
        if self.level == "people":
            return [
                [
                    (safe_display(peer), sender_style_name(peer, self.session.client.username)),
                    (f"  {count} msgs", "muted"),
                    ("  " + safe_display(text), "muted"),
                ]
                for peer, _stamp, text, count in self.people
            ]
        return [
            ([("current  ", "accent")] if r.id == self.session.client.session_id else [])
            + [(display_time(r.started), "text"), (f"  {r.count} msgs", "muted")]
            for r in self.records
        ]

    def page(self, width, visible):
        """Visible segment rows and the index of the first one."""
        width = max(1, width)
        if self.pending_delete is not None:
            lines = [
                (self.pending_delete[2], "title"),
                ("", ""),
                ("Only your saved copy is deleted.", "muted"),
                ("This cannot be undone.", "error"),
                ("", ""),
            ]
            if self.error:
                lines.insert(0, (self.error, "error"))
            rows = [
                [(part, style)]
                for line, style in lines
                for part in wrap_words(line, width)
            ]
            rows.append(fit_row([("Y", "key"), (" delete    ", "muted"),
                                 ("Esc", "key"), (" / ", "muted"), ("N", "key"),
                                 (" cancel", "muted")], width))
            return rows[:visible], 0
        if self.error:
            return [[(fit_cells(self.error, width), "error")]], 0
        if self.level == "messages" and self._width != width:
            self._rows = []
            self._row_messages = []
            records = {record.id: record for record in self.records}
            previous = None
            for index, (timestamp, direction, kind, peer, text) in enumerate(self.transcript):
                if self.session_ids and self.session_ids[index] != previous:
                    previous = self.session_ids[index]
                    record = records[previous]
                    label = f" Session · {display_time(record.started)} "
                    rule = max(0, width - cell_width(label) - 2)
                    self._rows.append(fit_row(
                        [("──", "faint"), (label, "muted"), ("─" * rule, "faint")], width
                    ))
                    self._row_messages.append(None)
                sender = f"you -> {peer}" if kind == "CHAT_PRIVATE" and not self.peer else "you"
                if kind == "CHAT_GROUP" and direction != "sent":
                    sender, _, text = text.partition(": ")
                card = message_rows(
                    display_time(timestamp),
                    sender if direction == "sent" or kind == "CHAT_GROUP" else peer,
                    text,
                    peer if kind == "CHAT_GROUP" else "DM" if kind == "CHAT_PRIVATE" else "All",
                    width,
                    self.session.client.username,
                )
                self._rows.extend(card)
                self._row_messages.extend(index if row_text(row).strip() else None for row in card)
            if not self._rows:
                self._rows = [[("No saved messages.", "muted")]]
            self._width = width
        if self.level == "messages":
            end = max(0, len(self._rows) - visible)
            if self._open_at_end:
                self.scroll = end
                self._open_at_end = False
            self.scroll = min(self.scroll, end)
            return self._rows[self.scroll : self.scroll + visible], self.scroll
        items = self.items()
        start = max(0, self.selected - visible + 1)
        page = []
        for i, row in enumerate(items):
            if not start <= i < start + visible:
                continue
            if i == self.selected:
                row = [("› ", "accent")] + [
                    (text, "muted" if style == "muted" else "selected") for text, style in row
                ]
            else:
                row = [("  ", "")] + row
            page.append(fit_row(row, width))
        empty = "No saved people yet." if self.level == "people" else "No saved sessions."
        return page or [[(empty, "muted")]], start

    def move(self, step):
        if self.level == "messages":
            self.scroll = max(0, min(len(self._rows), self.scroll + step))
        else:
            self.selected = max(0, min(len(self.items()) - 1, self.selected + step))

    def switch_list(self):
        if self.level != "messages":
            self.level = "people" if self.level == "sessions" else "sessions"
            self.selected = 0
            self.error = None

    def _open_session(self, session_id, peer=None):
        record, rows = self.session.history_transcript(session_id, peer)
        self.parents.append((self.level, self.selected))
        self.record, self.peer = record, peer
        self.transcript, self.session_ids = rows, []
        self.level, self.scroll, self._width = "messages", 0, None
        self._open_at_end = True

    def _open_person(self, peer):
        rows = self.session.history_archive(peer)
        self.parents.append((self.level, self.selected))
        self.record, self.peer = None, peer
        self.transcript = [row[1:] for row in rows]
        self.session_ids = [row[0] for row in rows]
        self.level, self.scroll, self._width = "messages", 0, None
        self._open_at_end = True

    def request_delete(self):
        """Freeze the selected scope; navigation cannot change a pending deletion."""
        session_id, peer = None, None
        if self.level == "sessions" and self.records:
            record = self.records[self.selected]
            session_id = record.id
            label = f"Delete session from {display_time(record.started)}?"
        elif self.level == "people" and self.people:
            peer = self.people[self.selected][0]
            label = f"Delete saved history with {safe_display(peer)}?"
        elif self.level == "messages" and (self.record is not None or self.peer is not None):
            session_id = self.record.id if self.record else None
            peer = self.peer
            label = (
                f"Delete saved messages with {safe_display(peer)}?"
                if peer
                else f"Delete session from {display_time(self.record.started)}?"
            )
            if peer and self.record:
                label += f" Only {display_time(self.record.started)}."
        else:
            return
        if session_id == self.session.client.session_id and peer is None:
            label = "Clear saved messages from your current session?"
        self.pending_delete = (session_id, peer, label)
        self.notice = self.error = None

    def confirm_delete(self):
        if self.pending_delete is None:
            return
        session_id, peer, _label = self.pending_delete
        count = self.session.delete_history(session_id=session_id, peer=peer)
        self.pending_delete = None
        if self.level == "messages":
            self.back()
        self.records = self.session.history_sessions()
        self.people = self.session.history_people()
        self.selected = min(self.selected, max(0, len(self.items()) - 1))
        self.transcript, self.session_ids, self._rows = [], [], []
        self._width = None
        self.notice = f"Deleted {count} saved message{'s' if count != 1 else ''}."

    def open(self):
        self.notice = self.error = None
        if self.level == "people" and self.people:
            self._open_person(self.people[self.selected][0])
        elif self.level == "sessions" and self.records:
            self._open_session(self.records[self.selected].id or "legacy")

    def back(self):
        self.error = None
        if not self.parents:
            return False
        self.level, self.selected = self.parents.pop()
        return True


class Composer:
    """Editable input and per-launch command recall, independent of curses."""

    def __init__(self):
        self.text = ""
        self.cursor = 0
        self.history = deque(maxlen=100)
        self.index = None
        self.draft = ""

    def insert(self, text):
        if len(self.text) + len(text) <= 8192:
            self.text = self.text[: self.cursor] + text + self.text[self.cursor :]
            self.cursor += len(text)
            self.index = None

    def replace(self, text):
        self.text, self.cursor = text, len(text)

    def recall(self, direction):
        if not self.history:
            return
        if self.index is None:
            self.draft = self.text
            self.index = len(self.history)
        self.index = max(0, min(len(self.history), self.index + direction))
        self.replace(self.draft if self.index == len(self.history) else self.history[self.index])

    def take(self):
        text = self.text.strip()
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
        self.replace("")
        self.index = None
        return text

    def delete_word(self):
        """Delete the word before the cursor, plus the spaces before it."""
        if not self.cursor:
            return
        end = self.cursor
        while end > 0 and self.text[end - 1] == " ":
            end -= 1
        while end > 0 and self.text[end - 1] != " ":
            end -= 1
        self.text = self.text[:end] + self.text[self.cursor :]
        self.cursor = end
        self.index = None


# Per theme: base foreground/background, then style name -> (256-color
# foreground, 8-color foreground, extra attributes). DIM8 dims only when
# the terminal has 8 colors and cannot show a softer gray.
THEMES = {
    "dark": {
        "base": (252, 234, "WHITE", None),
        "pill": (234, 173),
        "highlight": (231, 238),
        "styles": {
            "accent": (173, "CYAN", "BOLD"),
            "title": (173, "CYAN", "BOLD"),
            "success": (114, "GREEN", ""),
            "muted": (246, "WHITE", "DIM8"),
            "faint": (240, "WHITE", "DIM"),
            "heading": (250, "WHITE", "BOLD"),
            "error": (203, "RED", ""),
            "file": (179, "YELLOW", ""),
            "dm": (176, "MAGENTA", ""),
            "admin": (111, "BLUE", ""),
            "sender_a": (111, "BLUE", "BOLD"),
            "sender_b": (141, "MAGENTA", "BOLD"),
            "sender_c": (80, "CYAN", "BOLD"),
            "sender_d": (179, "YELLOW", "BOLD"),
            "sender_e": (150, "GREEN", "BOLD"),
        },
    },
    "light": {
        "base": (236, 255, "BLACK", "WHITE"),
        "pill": (231, 166),
        "highlight": (232, 252),
        "styles": {
            "accent": (166, "BLUE", "BOLD"),
            "title": (166, "BLUE", "BOLD"),
            "success": (28, "GREEN", ""),
            "muted": (243, "BLACK", "DIM8"),
            "faint": (249, "BLACK", "DIM"),
            "heading": (238, "BLACK", "BOLD"),
            "error": (160, "RED", ""),
            "file": (130, "YELLOW", ""),
            "dm": (127, "MAGENTA", ""),
            "admin": (25, "BLUE", ""),
            "sender_a": (25, "BLUE", "BOLD"),
            "sender_b": (91, "MAGENTA", "BOLD"),
            "sender_c": (30, "CYAN", "BOLD"),
            "sender_d": (130, "YELLOW", "BOLD"),
            "sender_e": (28, "GREEN", "BOLD"),
        },
    },
}


def _palette(curses, theme="dark"):
    """Curses attributes per style: 256 colors, 8 colors, or emphasis only."""
    spec = THEMES.get(theme, THEMES["dark"])
    styles = {name: 0 for name in spec["styles"]}
    styles.update(
        text=0, key=curses.A_BOLD, selected=curses.A_REVERSE, pill=curses.A_REVERSE,
        highlight=curses.A_REVERSE,
    )
    styles["heading"] = curses.A_BOLD
    styles["accent"] = styles["title"] = curses.A_BOLD
    styles["muted"] = styles["faint"] = curses.A_DIM
    styles["dm"] = curses.A_BOLD
    for name in SENDER_STYLES:
        styles[name] = curses.A_BOLD
    try:
        if "NO_COLOR" in os.environ or not curses.has_colors():
            return styles
        curses.start_color()
        default_background = curses.COLOR_BLACK
        try:
            curses.use_default_colors()
            default_background = -1
        except curses.error:
            pass
        rich = getattr(curses, "COLORS", 8) >= 256 and getattr(curses, "COLOR_PAIRS", 0) > 32
        fine_text, fine_background, basic_text, basic_background = spec["base"]
        # 256 colors paint the theme's own background; 8 colors keep the
        # terminal's dark background unless the light theme asks for white.
        background = (
            fine_background if rich
            else getattr(curses, "COLOR_" + basic_background) if basic_background
            else default_background
        )
        number = 0

        def pair(foreground, back=background):
            nonlocal number
            number += 1
            curses.init_pair(number, foreground, back)
            return curses.color_pair(number)

        styles["text"] = pair(fine_text if rich else getattr(curses, "COLOR_" + basic_text))
        for name, (fine, basic, extra) in spec["styles"].items():
            attributes = curses.A_BOLD if extra == "BOLD" else 0
            if extra == "DIM" or (extra == "DIM8" and not rich):
                attributes |= curses.A_DIM
            styles[name] = pair(fine if rich else getattr(curses, "COLOR_" + basic)) | attributes
        styles["key"] = styles["text"] | curses.A_BOLD
        styles["selected"] = styles["accent"] | curses.A_BOLD
        if rich:
            styles["pill"] = pair(*spec["pill"]) | curses.A_BOLD
            styles["highlight"] = pair(*spec["highlight"]) | curses.A_BOLD
        else:
            styles["selected"] |= curses.A_REVERSE
            styles["pill"] = styles["accent"] | curses.A_REVERSE
            styles["highlight"] = styles["accent"] | curses.A_REVERSE
    except curses.error:
        pass  # Monochrome terminals retain emphasis and selection.
    return styles


def apply_theme(curses, window, theme):
    """Palette plus the window background, so empty cells use the theme too."""
    styles = _palette(curses, theme)
    try:
        window.bkgd(" ", styles["text"])
    except (AttributeError, TypeError, curses.error):
        pass
    return styles


def help_lines(width, role="member"):
    """Full-screen help content as (text, style) rows, wrapped to width.

    Built from the team's own _DETAILED_HELP reference, so this screen and
    /help in the plain CLI can never drift apart. F1 or /help opens this
    view; Ctrl+C (or Esc) returns to the chat.
    """
    width = max(10, width)
    rows = [("LAN COLLAB — Help", "title"), ("", "")]
    for line in help_reference(role):
        if not line:
            rows.append(("", ""))
            continue
        style = (
            "muted" if line.startswith("  ")
            else "command" if line.startswith("/")
            else "section" if line.isupper() or line.startswith("ADMIN ONLY")
            else "muted"
        )
        indent = "  " if line.startswith("  ") else ""
        for index, part in enumerate(wrap_words(line.strip(), width - len(indent))):
            part = indent + part
            rows.append((part, style if index == 0 or style != "command" else "text"))
    rows.append(("", ""))
    rows.append(("Ctrl+C / Esc — back to chat", "muted"))
    return rows


def help_segments(text, style):
    """Commands are highlighted up to their em dash; sections stand out."""
    if style == "command":
        command, dash, rest = text.partition(" — ")
        return [(command, "accent"), (dash + rest, "text")]
    if style == "section":
        return [(text, "title")]
    return [(text, style or "text")]


TABS = (
    ("all", "All"),
    ("global", "Global"),
    ("groups", "Groups"),
    ("dm", "Direct"),
    ("files", "Files"),
)


def hint_row(pairs):
    """Footer hints: keys in bold, actions dimmed, separated by thin dots."""
    row = []
    for key, label in pairs:
        if row:
            row.append(("  ·  ", "faint"))
        row += [(key, "key"), (" " + label, "muted")]
    return row


DESTRUCTIVE = ("Delete", "Kick", "Reject", "Confirm delete", "Confirm leave")


def run_tui(session):
    import curses

    notices = Queue()
    original_output = session.output
    session.output = notices.put

    def screen(window):
        theme = load_theme()
        styles = apply_theme(curses, window, theme)
        window.timeout(100)
        window.keypad(True)
        mouse_mask = (
            getattr(curses, "BUTTON1_CLICKED", 0)
            | getattr(curses, "BUTTON3_CLICKED", 0)
            | getattr(curses, "BUTTON3_PRESSED", 0)
            | getattr(curses, "BUTTON4_PRESSED", 0)
            | getattr(curses, "BUTTON5_PRESSED", 0)
        )
        try:
            curses.mousemask(mouse_mask)
        except curses.error:
            pass  # Mouse-less terminals keep keyboard navigation.
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        composer = Composer()
        scroll, selection, dismissed = 0, 0, False
        # Direct tab: None shows the inbox; a peer name shows that transcript.
        # Reads are cached against client.direct_revision, never polled per frame.
        direct_peer = None
        direct_selected = direct_scroll = direct_unseen = 0
        group_selected = 0
        group_page_key, group_page_rows = None, None
        direct_conversations = None
        direct_conversations_revision = None
        direct_page_rows = None
        direct_page_revision = None
        direct_page_error = None
        direct_dirty = True
        direct_flash = None  # (text, style, expires)
        drafts = {}
        context_key = "dm-inbox" if session.display_filter == "dm" else "main"
        last_filter = session.display_filter
        show_help, help_scroll = False, 0
        history_browser = None
        copy_picker = None
        sidebar_enabled = True
        # Transient context menu: kind, target, index, anchor. Rebuilt and
        # validated on every frame so resizes and disappearing people are safe.
        menu = None
        clear_generation = session.clear_generation
        next_pending_refresh = monotonic() + 10
        probe_worker = None
        # Incoming chats that arrived in a tab you were not looking at.
        unread = {"global": 0, "groups": 0, "dm": 0}

        def probe():
            session.execute(parse_command("/ping"))
            if session.udp is not None:
                session.execute(parse_command("/net"))

        def start_probe():
            nonlocal probe_worker
            if probe_worker is None or not probe_worker.is_alive():
                probe_worker = Thread(target=probe, daemon=True, name="tui-probe")
                probe_worker.start()

        unseen = 0  # Notices that arrived while scrolled up.
        messages = deque(maxlen=2000)

        def add(text, kind="muted", stamp=True):
            nonlocal direct_flash
            if kind == "error" and session.display_filter == "groups" and getattr(session, "current_group", None) is None:
                direct_flash = (safe_display(text), "error", monotonic() + 10)
            # Only real events get a time prefix. Help text, history rows
            # (which carry their own timestamp) and plain notices do not.
            messages.append(
                (
                    datetime.now().strftime("%H:%M") if stamp else "",
                    safe_display(text),
                    kind,
                    getattr(text, "copy_text", None),
                )
            )

        messages.append(("", "", "welcome", None))

        def write(y, x, text, width, style="muted"):
            if width <= 0:
                return
            try:
                window.addstr(y, x, fit_cells(text, width), styles.get(style, 0))
            except curses.error:
                pass  # Resizes and the bottom-right cell can interrupt a draw.

        def put(y, x, row, width):
            """Draw a segment row clipped to width; return the x after it."""
            for text, style in fit_row(row, width):
                write(y, x, text, cell_width(text), style)
                x += cell_width(text)
            return x

        def box(top, left, height, box_width, style, title=(), right=(), footnote=()):
            """Rounded frame with a title, a right-hand label and a bottom-right note."""
            inner = box_width - 2
            if inner < 2 or height < 2:
                return
            write(top, left, "╭" + "─" * inner + "╮", box_width, style)
            for y in range(top + 1, top + height - 1):
                write(y, left, "│" + " " * inner + "│", box_width, style)
            write(top + height - 1, left, "╰" + "─" * inner + "╯", box_width, style)
            used = 0
            if title:
                used = put(top, left + 2, [(" ", style), *title, (" ", style)], inner - 2) - left
            if right:
                right = fit_row([(" ", style), *right, (" ", style)], inner - used - 1)
                if right:
                    put(top, left + box_width - 2 - cell_width(row_text(right)), right, inner)
            if footnote:
                footnote = fit_row([(" ", style), *footnote, (" ", style)], inner - 2)
                put(top + height - 1, left + box_width - 2 - cell_width(row_text(footnote)),
                    footnote, inner)

        def chrome(title, crumb="", buttons=()):
            """Title row and rule for full-screen views; returns button positions."""
            height, width = window.getmaxyx()
            x, placed = width - 2, {}
            for label, style in reversed(buttons):
                x -= cell_width(label)
                write(0, x, label, cell_width(label), style)
                placed[label] = x
                x -= 2
            row = [("✻ ", "accent"), (title, "title")]
            if crumb:
                row.append(("  " + crumb, "muted"))
            put(0, 2, row, x - 2)
            write(1, 2, "─" * (width - 4), width - 4, "faint")
            return placed

        def footer(left, right=()):
            height, width = window.getmaxyx()
            room = width - 4
            right = fit_row(list(right), room)
            right_width = cell_width(row_text(right))
            room -= right_width + (2 if right_width else 0)
            # Drop whole hints rather than cutting one in half.
            while cell_width(row_text(left)) > room and ("  ·  ", "faint") in left:
                left = left[: len(left) - 1 - left[::-1].index(("  ·  ", "faint"))]
            if left and left[0][1] == "key" and cell_width(row_text(left)) > room:
                left = []
            put(height - 1, 2, left, room)
            if right:
                put(height - 1, width - 2 - right_width, right, right_width)

        def run_command(line):
            """Dispatch through the same backend as typed commands; False means quit."""
            nonlocal direct_flash
            try:
                if not session.execute(parse_command(line)):
                    return False
            except ValueError as error:
                message = safe_display(str(error))
                add(message, "error")
                if session.display_filter in {"dm", "groups"}:
                    direct_flash = (message, "error", monotonic() + 10)
            return True

        def select_filter(category):
            session.display_filter = category
            if category == "groups" and getattr(session, "current_group", None) not in groups:
                session.current_group = None
                if len(groups) == 1:
                    run_command(f"/group open {next(iter(groups))}")

        def cycle_filter(step):
            nonlocal scroll
            select_filter(FILTERS[(FILTERS.index(session.display_filter) + step) % len(FILTERS)])
            scroll = 0

        def native_picker(*, folder=False):
            """Release the terminal for the desktop dialog, then restore input."""
            curses.def_prog_mode()
            curses.endwin()
            try:
                return choose_file(folder=folder)
            finally:
                curses.reset_prog_mode()
                window.keypad(True)
                window.timeout(100)
                try:
                    # Force re-enabling reporting even if curses cached the mask.
                    curses.mousemask(0)
                    curses.mousemask(mouse_mask)
                    curses.curs_set(1)
                except curses.error:
                    pass
                window.clearok(True)
                window.refresh()

        def switch_context(key):
            """Save the composer under the current context, then load another.

            Direct drafts are keyed by peer, so text typed for one conversation
            can never become a global message after a tab or peer switch.
            """
            nonlocal context_key, selection, dismissed, scroll
            if key == context_key:
                return
            drafts[context_key] = composer.text
            context_key = key
            composer.replace(drafts.get(key, ""))
            selection, dismissed = 0, False
            scroll = 0

        def reset_direct_page():
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll, direct_unseen
            direct_peer = None
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = direct_unseen = 0

        def open_direct(peer):
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll, direct_unseen, last_filter, direct_dirty
            nonlocal direct_flash
            if not valid_username(peer):
                return False
            switch_context(f"direct:{peer}")
            direct_peer = peer
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = direct_unseen = 0
            direct_dirty = True
            direct_flash = None
            session.display_filter = "dm"
            last_filter = "dm"
            return True

        def back_to_inbox():
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll, direct_unseen
            if direct_peer is None:
                return
            switch_context("dm-inbox")
            direct_peer = None
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = direct_unseen = 0

        def refresh_direct():
            """Re-read Direct data only when the backend revision or a notice changed it."""
            nonlocal direct_conversations, direct_conversations_revision
            nonlocal direct_page_rows, direct_page_revision, direct_page_error
            nonlocal direct_selected, direct_dirty, direct_flash
            revision = getattr(session.client, "direct_revision", None)
            previous = None
            if direct_conversations and 0 <= direct_selected < len(direct_conversations):
                previous = direct_conversations[direct_selected][0]
            if (
                direct_dirty
                or direct_conversations is None
                or (revision is not None and revision != direct_conversations_revision)
            ):
                try:
                    direct_conversations = [tuple(row) for row in session.direct_conversations()]
                except (ValueError, RuntimeError, OSError, sqlite3.Error):
                    direct_conversations = direct_conversations or []
                    direct_flash = (
                        "Could not load conversations; showing the last known list.",
                        "error",
                        monotonic() + 10,
                    )
                direct_conversations_revision = revision
                if previous is not None:
                    peers = [row[0] for row in direct_conversations]
                    if previous in peers:
                        direct_selected = peers.index(previous)
            if direct_conversations:
                direct_selected = max(0, min(len(direct_conversations) - 1, direct_selected))
            else:
                direct_selected = 0
            if direct_peer is not None and (
                direct_dirty
                or direct_page_rows is None
                or (revision is not None and revision != direct_page_revision)
            ):
                try:
                    direct_page_rows = [
                        tuple(row) for row in session.direct_transcript(direct_peer)
                    ]
                    direct_page_error = None
                except (ValueError, RuntimeError, OSError, sqlite3.Error) as error:
                    direct_page_rows = None
                    direct_page_error = safe_display(str(error))
                direct_page_revision = revision
            direct_dirty = False

        def perform(action):
            """Apply a menu/button action built from the current frame."""
            nonlocal menu, scroll, dismissed
            if action[0] == "command":
                menu = None
                return run_command(action[1])
            if action[0] == "group_menu":
                menu = ["group", action[1], 0, None, None]
                return True
            if action[0] == "confirm_group":
                menu = ["group_confirm", (action[1], action[2]), 1, None, None]
                return True
            if action[0] == "group_add":
                menu = None
                if composer.text:
                    add("Finish or clear your draft before adding a member.", "error")
                else:
                    composer.replace(f"/group add {action[1]} ")
                return True
            if action[0] == "close_menu":
                menu = None
                return True
            if action[0] == "direct_peer":
                menu = None
                return open_direct(action[1])
            if action[0] == "direct_back":
                menu = None
                back_to_inbox()
                return True
            if action[0] == "group_back":
                menu = None
                session.current_group = None
                return True
            if action[0] == "accept_file":
                menu = None
                try:
                    directory = native_picker(folder=True)
                    if directory:
                        session.client.files.accept(action[1], directory=directory)
                except (RuntimeError, ValueError, OSError) as error:
                    add(str(error), "error")
                finally:
                    window.clearok(True)
                return True
            if action[0] in {"send_file", "send_group_file"}:
                menu = None
                try:
                    path = native_picker()
                    if path:
                        if action[0] == "send_group_file":
                            session.execute(Command("/sendgroup", (action[1], path)))
                        else:
                            with session.client._lock:
                                online = action[1] in session.client.online_users
                            if not online:
                                add("Recipient went offline; no file sent.", "error")
                            else:
                                session.execute(Command("/send", (action[1], path)))
                except (RuntimeError, OSError) as error:
                    add(str(error), "error")
                finally:
                    window.clearok(True)
                return True
            if action[0] == "open_user":
                menu = ["user", action[1], 0, None, None]
            elif action[0] == "open_pending":
                menu = ["pending", action[1], 0, None, None]
            return True

        def hit_at(x, y):
            """Topmost drawn hitbox; rebuilt every frame, so resize-safe."""
            for row, left, right, action in reversed(hits):
                if row == y and left <= x < right:
                    return action
            return None

        while True:
            if session.client.state is ClientState.CLOSED:
                show_help = False
                history_browser = copy_picker = menu = None
                session.display_filter = "all"
            if session.client.state is not ClientState.CLOSED and monotonic() >= next_pending_refresh:
                if session.client.role == "admin":
                    # Quiet refresh: update the sidebar's PENDING list without
                    # printing a "Pending users" line into the chat each time.
                    session.refresh_pending_quietly()
                next_pending_refresh = monotonic() + 10
            if session.clear_generation != clear_generation:
                messages.clear()
                scroll = unseen = 0
                clear_generation = session.clear_generation
            while True:
                try:
                    notice = notices.get_nowait()
                    if notice == "[presence] Online list refreshed" or notice.startswith(
                        "[auth] Authenticated as "
                    ):
                        continue
                    kind = (
                        "error"
                        if notice.startswith(("[error]", "[auth_error]", "[protocol_error]"))
                        else "file"
                        if notice.startswith("[file]")
                        else "dm"
                        if notice_category(notice) == "dm"
                        else "admin"
                        if notice.startswith("[admin]")
                        else "success"
                        if notice.startswith("[chat]")
                        else "muted"
                    )
                    if kind == "error":
                        notice = re.sub(r"^\[(?:error|auth_error|protocol_error)\] ", "", notice)
                    add(notice, kind, stamp=notice.startswith("[") or kind == "error")
                    if kind == "dm":
                        direct_dirty = True
                    if (
                        (
                            session.display_filter == "dm"
                            or (session.display_filter == "groups"
                                and getattr(session, "current_group", None) is None)
                        )
                        and notice_category(notice) not in {"dm", "global"}
                        and not notice.startswith("[presence]")
                    ):
                        # Errors and send results stay visible even though the
                        # Direct view replaces ordinary scrollback.
                        direct_flash = (
                            safe_display(re.sub(r"^\[file\] ", "", notice)),
                            "error" if kind == "error" else kind,
                            monotonic() + 10,
                        )
                    category = notice_category(notice)
                    incoming = chat_sender(notice) not in (None, "you") and not (
                        chat_sender(notice) or ""
                    ).startswith("you ->")
                    if incoming and category in unread and session.display_filter not in {
                        "all", category
                    }:
                        unread[category] += 1
                    if (
                        scroll
                        and incoming
                        and category in {"global", "dm", "groups"}
                        and session.display_filter in {"all", category}
                        and (
                            category != "groups" or session.display_filter == "all"
                            or chat_group(notice) == getattr(session, "current_group", None)
                        )
                    ):
                        unseen += 1
                    if (
                        direct_scroll
                        and incoming
                        and category == "dm"
                        and chat_sender(notice) == direct_peer
                    ):
                        direct_unseen += 1
                except Empty:
                    break
            if session.display_filter in unread:
                unread[session.display_filter] = 0
            if session.display_filter != last_filter:
                # Leaving Direct must never keep a peer route (or its draft)
                # active; drafts are stored per peer and restored on return.
                reset_direct_page()
                last_filter = session.display_filter
                menu = None
                direct_flash = None
                scroll = 0
                switch_context("dm-inbox" if last_filter == "dm" else "main")
            if session.display_filter == "groups":
                switch_context(f"group:{getattr(session, 'current_group', None)}")
            if copy_picker is not None:
                height, width = window.getmaxyx()
                window.erase()
                count = len(copy_picker.messages)
                chrome(
                    "Copy message",
                    f"{copy_picker.selected + 1} of {count}"
                    if count
                    else "No messages to copy in this view.",
                )
                list_size = min(5, max(1, height // 4))
                start = max(0, copy_picker.selected - list_size + 1)
                for y, index in enumerate(range(start, min(count, start + list_size)), 3):
                    entry = copy_picker.messages[index]
                    chosen = index == copy_picker.selected
                    put(
                        y,
                        2,
                        [
                            ("› " if chosen else "  ", "accent"),
                            (safe_display(entry.label), "selected" if chosen else "muted"),
                            ("  " + safe_display(entry.text), "text" if chosen else "muted"),
                        ],
                        width - 4,
                    )
                preview_y = 4 + list_size
                write(preview_y - 1, 2, "─" * max(0, width - 4), width - 4, "faint")
                visible = max(1, height - preview_y - 4)
                preview = (
                    wrap_words(
                        safe_display(copy_picker.messages[copy_picker.selected].text),
                        max(1, width - 4),
                    )
                    if count
                    else []
                )
                copy_picker.scroll = max(0, min(copy_picker.scroll, len(preview) - visible))
                for y, line in enumerate(
                    preview[copy_picker.scroll : copy_picker.scroll + visible], preview_y
                ):
                    write(y, 2, line, width - 4, "text")
                if copy_picker.notice:
                    feedback, style = copy_picker.notice
                    for y, line in enumerate(
                        wrap_words(safe_display(feedback), max(1, width - 4))[:2], height - 4
                    ):
                        write(y, 2, ("✓ " if style == "success" else "✗ ") + line, width - 4, style)
                write(height - 2, 2, "[Copy message]" if count else "", width - 4, "accent")
                footer(hint_row([("↑↓", "choose"), ("c/Enter", "copy"),
                                 ("PgUp/PgDn", "preview"), ("Esc", "back")]))
                try:
                    curses.curs_set(0)
                    window.refresh()
                    key = window.get_wch()
                    if key in ("\x1b", "\x03"):
                        copy_picker = None
                        curses.curs_set(1)
                    elif key in ("c", "C", "\n", "\r", curses.KEY_ENTER):
                        copy_picker.copy()
                    elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END):
                        copy_picker.move(
                            -1
                            if key == curses.KEY_UP
                            else 1
                            if key == curses.KEY_DOWN
                            else -(10**9)
                            if key == curses.KEY_HOME
                            else 10**9
                        )
                    elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                        copy_picker.scroll += -visible if key == curses.KEY_PPAGE else visible
                    elif key == curses.KEY_MOUSE:
                        _, mx, my, _, button = curses.getmouse()
                        if button & getattr(curses, "BUTTON4_PRESSED", 0):
                            copy_picker.scroll -= 3
                        elif button & getattr(curses, "BUTTON5_PRESSED", 0):
                            copy_picker.scroll += 3
                        elif button & getattr(curses, "BUTTON1_CLICKED", 0):
                            if my == height - 2 and 2 <= mx < 16:
                                copy_picker.copy()
                            elif (
                                3 <= my < 3 + min(count - start, list_size) and 2 <= mx < width - 2
                            ):
                                copy_picker.move(start + my - 3 - copy_picker.selected)
                except curses.error:
                    pass
                continue
            if history_browser is not None:
                height, width = window.getmaxyx()
                window.erase()
                visible = max(1, height - 5)
                can_copy = (
                    history_browser.level == "messages"
                    and not history_browser.error
                    and history_browser.pending_delete is None
                )
                placed = chrome(
                    "History",
                    history_browser.breadcrumb,
                    ([("[Copy]", "accent")] if can_copy else []) + [("[Delete]", "error")],
                )
                list_tabs = []
                if history_browser.level in {"sessions", "people"}:
                    x = 2
                    for level, label in (("sessions", " Sessions "), ("people", " People ")):
                        style = "pill" if history_browser.level == level else "muted"
                        write(2, x, label, len(label), style)
                        list_tabs.append((x, x + len(label), level))
                        x += len(label) + 1
                    subtitle = (
                        "Choose a person" if history_browser.level == "people"
                        else "Your login sessions"
                    )
                    put(3, 2, [(history_browser.notice or subtitle,
                                "success" if history_browser.notice else "muted")], width - 4)
                elif history_browser.notice:
                    write(2, 2, "✓ " + history_browser.notice, width - 4, "success")
                page, start = history_browser.page(width - 4, visible)
                for y, row in enumerate(page, 4):
                    put(y, 2, row, width - 4)
                hint = (
                    hint_row([("↑↓", "scroll"), ("F9", "copy"), ("Del", "delete"), ("Esc", "back")])
                    if history_browser.level == "messages"
                    else hint_row([("↑↓", "choose"), ("Enter", "open"), ("Tab", "sessions/people"),
                                   ("Del", "delete"), ("Esc", "back")])
                )
                if history_browser.pending_delete is not None:
                    hint = hint_row([("Y", "delete"), ("Esc/N", "cancel")])
                footer(hint)
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                try:
                    window.refresh()
                    key = window.get_wch()
                except KeyboardInterrupt:
                    key = "\x03"
                except curses.error:
                    continue
                try:
                    if history_browser.pending_delete is not None:
                        if key in ("y", "Y"):
                            history_browser.confirm_delete()
                        elif key in ("\x1b", "\x03", "n", "N"):
                            history_browser.pending_delete = None
                            history_browser.error = None
                        continue
                    if key == getattr(curses, "KEY_F9", -9) and can_copy:
                        copy_picker = CopyPicker(
                            transcript_copy_messages(history_browser.transcript)
                        )
                    elif key == curses.KEY_DC:
                        history_browser.request_delete()
                    elif key in ("\x1b", "\x03"):
                        if not history_browser.back():
                            history_browser = None
                            curses.curs_set(1)
                    elif key in (
                        curses.KEY_UP,
                        curses.KEY_DOWN,
                        curses.KEY_PPAGE,
                        curses.KEY_NPAGE,
                    ):
                        step = (
                            -1
                            if key == curses.KEY_UP
                            else 1
                            if key == curses.KEY_DOWN
                            else -visible
                            if key == curses.KEY_PPAGE
                            else visible
                        )
                        history_browser.move(step)
                    elif key == "\t":
                        history_browser.switch_list()
                    elif key in (curses.KEY_HOME, curses.KEY_END):
                        history_browser.move(-(10**9) if key == curses.KEY_HOME else 10**9)
                    elif key in ("\n", "\r", curses.KEY_ENTER):
                        history_browser.open()
                    elif key == curses.KEY_MOUSE:
                        _, mx, my, _, button = curses.getmouse()
                        clicked = button & getattr(curses, "BUTTON1_CLICKED", 0)
                        delete_x = placed.get("[Delete]")
                        copy_x = placed.get("[Copy]")
                        if clicked and my == 0 and delete_x is not None and delete_x <= mx < delete_x + 8:
                            history_browser.request_delete()
                        elif clicked and my == 0 and copy_x is not None and copy_x <= mx < copy_x + 6:
                            copy_picker = CopyPicker(
                                transcript_copy_messages(history_browser.transcript)
                            )
                        elif clicked and my == 2 and list_tabs:
                            for left, right, level in list_tabs:
                                if left <= mx < right and level != history_browser.level:
                                    history_browser.switch_list()
                        elif clicked and can_copy and 2 <= mx < width - 2 and 4 <= my < 4 + len(page):
                            index = start + my - 4
                            mapping = getattr(history_browser, "_row_messages", [])
                            if index < len(mapping) and mapping[index] is not None:
                                copy_picker = CopyPicker(
                                    transcript_copy_messages(history_browser.transcript),
                                    mapping[index],
                                )
                        elif button & getattr(curses, "BUTTON4_PRESSED", 0):
                            history_browser.move(-3)
                        elif button & getattr(curses, "BUTTON5_PRESSED", 0):
                            history_browser.move(3)
                        elif (
                            clicked
                            and 2 <= mx < width - 2
                            and 4 <= my < 4 + len(page)
                            and history_browser.level != "messages"
                        ):
                            history_browser.selected = start + my - 4
                            history_browser.open()
                except (ValueError, OSError, sqlite3.Error) as error:
                    history_browser.error = safe_display(str(error))
                except curses.error:
                    pass
                # Read-only: typing here never reaches the live composer.
                continue
            if show_help:
                height, width = window.getmaxyx()
                window.erase()
                chrome("Help", "commands and shortcuts")
                rows = help_lines(max(10, width - 4), session.client.role)[2:]
                visible = max(1, height - 4)
                help_scroll = min(help_scroll, max(0, len(rows) - visible))
                for y, (line, style) in enumerate(rows[help_scroll : help_scroll + visible], 2):
                    put(y, 2, help_segments(line, style), width - 4)
                position = (
                    f"{min(100, 100 * (help_scroll + visible) // max(1, len(rows)))}%"
                    if len(rows) > visible else ""
                )
                footer(hint_row([("↑↓/PgUp/PgDn", "scroll"), ("Esc", "back to chat")]),
                       [(position, "faint")])
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                try:
                    window.refresh()
                    key = window.get_wch()
                except KeyboardInterrupt:
                    # Ctrl+C arrives as SIGINT under cbreak, not as the \x03
                    # byte: treat it as "back to chat" instead of exiting.
                    key = "\x03"
                except curses.error:
                    continue
                if key in ("\x03", "\x1b") or key == getattr(curses, "KEY_F1", -1):
                    show_help = False
                    help_scroll = 0
                    try:
                        curses.curs_set(1)
                    except curses.error:
                        pass
                elif key == curses.KEY_UP:
                    help_scroll = max(0, help_scroll - 1)
                elif key == curses.KEY_DOWN:
                    help_scroll += 1
                elif key == curses.KEY_PPAGE:
                    help_scroll = max(0, help_scroll - visible)
                elif key == curses.KEY_NPAGE:
                    help_scroll += visible
                elif key in (curses.KEY_HOME, curses.KEY_END):
                    help_scroll = 0 if key == curses.KEY_HOME else len(rows)
                continue
            client = session.client
            with client._lock:
                username, role = client.username, client.role
                users = sorted(name for name in client.online_users if name != username)
                pending = [item["username"] for item in client.pending_users]
                reply_target = client.reply_target
                groups = dict(getattr(client, "groups", {}))
                group_owners = dict(getattr(client, "group_owners", {}))
            current_group = getattr(session, "current_group", None)
            if current_group is None or session.display_filter != "groups":
                group_page_key, group_page_rows = None, None
            elif session.display_filter == "groups" and hasattr(session, "group_transcript"):
                key = (current_group, getattr(client, "group_revision", None))
                if key != group_page_key:
                    try:
                        group_page_rows = list(session.group_transcript(current_group))
                    except (OSError, sqlite3.Error, RuntimeError, ValueError):
                        group_page_rows = None
                        direct_flash = (
                            "Could not read saved group history; showing live messages.",
                            "error", monotonic() + 10,
                        )
                    group_page_key = key
            group_names = sorted(groups)
            group_selected = max(0, min(group_selected, len(group_names) - 1))
            transfers = client.files.snapshot()
            options = (
                suggestions(composer.text, users, role, pending, transfers) if not dismissed else []
            )
            selection = min(selection, max(0, len(options) - 1))
            height, width = window.getmaxyx()
            window.erase()
            if height < 12 or width < 40:
                write(0, 0, "✻ Enlarge the terminal to 40 × 12", width - 1, "accent")
                write(1, 0, "Ctrl+C exits", width - 1, "muted")
                window.refresh()
                try:
                    if window.get_wch() in ("\x03", "\x04"):
                        return
                except curses.error:
                    pass
                continue
            # Geometry: header 0, tabs 1, rule 2, content from 3, then the
            # optional dock and suggestions, the input box and the footer.
            sidebar = 26 if sidebar_enabled and width >= 80 else 0
            main_right = width - sidebar if sidebar else width
            chat_width = main_right - 4
            box_top = height - 4
            input_y = height - 3
            popup_height = min(6, len(options), max(0, height - 12))
            popup_y = box_top - popup_height
            # Offers sort first so active transfers cannot push Accept buttons away.
            dock_rows = sorted(transfers, key=lambda row: row["status"] != "offer")
            dock_height = (
                0 if sidebar else min(3, len(dock_rows), max(0, height - popup_height - 10))
            )
            dock_y = popup_y - dock_height
            if direct_scroll == 0:
                direct_unseen = 0
            new_count = direct_unseen if direct_peer is not None else unseen if scroll else 0
            server, connection, rtt, loss = network_status(
                client, session.udp, getattr(session, "server_host", None)
            )
            closed = client.state is ClientState.CLOSED
            hits = []
            copy_entries = []
            row_copy_indices = []

            # Header: brand and identity on the left, live connection on the right.
            left = [("✻ ", "accent"), ("LAN Collab", "title"), ("  ", ""),
                    (safe_display(username or "disconnected"), "text")]
            if role == "admin":
                left.append((" · admin", "admin"))
            left.append((f" · {len(users)} online", "muted"))
            dot = [("● ", "error" if closed else "success"
                    if client.state is ClientState.AUTHENTICATED else "file")]
            for right in (
                dot + [(connection, "muted"), ("  ", ""), (rtt, "muted"), ("  ", ""), (server, "faint")],
                dot + [(connection, "muted"), ("  ", ""), (rtt, "muted")],
                dot + [(connection, "muted")],
                [],
            ):
                if cell_width(row_text(left + right)) + 2 <= width - 4:
                    break
            put(0, 2, spread_row(left, right, width - 4), width - 4)
            # Without room in the header, the server address rides on the input box.
            server_note = [] if (server, "faint") in right else [(server, "faint")]

            def tab_strip(padded):
                row, spans, x = [], [], 0
                for category, label in TABS:
                    active = session.display_filter == category
                    segments = [(f" {label} " if padded or active else label,
                                 "pill" if active else "muted")]
                    count = unread.get(category, 0)
                    if count and not active:
                        segments.append((f"{'' if padded else ' '}{min(count, 99)}", "accent"))
                    size = cell_width(row_text(segments))
                    spans.append((x, x + size, category))
                    row += segments + [(" ", "")]
                    x += size + 1
                return row, x - 1, spans

            strip, strip_width, spans = tab_strip(True)
            if strip_width > chat_width:
                strip, strip_width, spans = tab_strip(False)
            put(1, 2, strip, chat_width)
            for start_x, end_x, category in spans:
                hits.append((1, 2 + start_x, 2 + end_x, ("filter", category)))
            write(2, 2, "─" * (width - 4), width - 4, "faint")

            def append_card(rows, card, label, text):
                index = len(copy_entries)
                copy_entries.append(CopyMessage(label, text))
                rows.extend(card)
                row_copy_indices.extend(index if row_text(row).strip() else None for row in card)

            def append_plain(rows, extra):
                rows.extend(extra)
                row_copy_indices.extend([None] * len(extra))

            def draw_rows(rows, top, offset):
                """Newest rows at the bottom of the view; offset counts rows scrolled up."""
                capacity = max(1, dock_y - top)
                offset = min(offset, max(0, len(rows) - capacity))
                end = len(rows) - offset
                first = max(0, end - capacity)
                for y, row in enumerate(rows[first:end], top):
                    index = first + y - top
                    if index < len(row_copy_indices) and row_copy_indices[index] is not None:
                        hits.append((y, 2, 2 + chat_width, ("copy_message", row_copy_indices[index])))
                    put(y, 2, row, chat_width)
                return offset

            def header_bar(y, left, buttons=()):
                """Page header with right-aligned buttons that fit beside it."""
                x = 2 + chat_width
                room = 2 + cell_width(row_text(left)) + 2
                for label, style, action in reversed(buttons):
                    if x - cell_width(label) < room:
                        break
                    x -= cell_width(label)
                    write(y, x, label, cell_width(label), style)
                    hits.append((y, x, x + cell_width(label), action))
                    x -= 1
                put(y, 2, left, x - 2)

            content_capacity = max(1, dock_y - 4)
            if session.display_filter == "dm":
                refresh_direct()
                if direct_peer is None:
                    header_bar(3, [("Conversations", "title"),
                                   (f"  {len(direct_conversations or [])}", "faint")])
                    if not direct_conversations:
                        put(4, 2, [("No private conversations yet.", "muted")], chat_width)
                        put(5, 2, [("Pick someone in ONLINE or press ", "muted"), ("F7", "key"),
                                   (" to start one.", "muted")], chat_width)
                    else:
                        visible = max(1, content_capacity // 2)
                        start = max(
                            0,
                            min(
                                direct_selected - visible + 1,
                                len(direct_conversations) - visible,
                            ),
                        )
                        for slot, index in enumerate(
                            range(start, min(len(direct_conversations), start + visible))
                        ):
                            y = 4 + 2 * slot
                            peer, timestamp, text = direct_conversations[index]
                            chosen = index == direct_selected
                            online = peer in users
                            name = [
                                ("› " if chosen else "  ", "accent"),
                                ("● " if online else "○ ", "success" if online else "faint"),
                                (safe_display(peer),
                                 "selected" if chosen else sender_style_name(peer, username)),
                            ]
                            put(y, 2, spread_row(name, [(display_time(timestamp), "muted")],
                                                 chat_width), chat_width)
                            put(y + 1, 2, [("    ", ""), (safe_display(text),
                                                         "text" if chosen else "muted")],
                                chat_width)
                            hits.append((y, 2, 2 + chat_width, ("direct_peer", peer)))
                            hits.append((y + 1, 2, 2 + chat_width, ("direct_peer", peer)))
                else:
                    online = direct_peer in users
                    header_bar(
                        3,
                        [
                            ("← Back", "accent"),
                            ("   ", ""),
                            ("● " if online else "○ ", "success" if online else "faint"),
                            (safe_display(direct_peer), sender_style_name(direct_peer, username)),
                            ("  " + ("online" if online else "offline · messages wait for them"),
                             "muted"),
                        ],
                        [("[Send file]", "accent", ("send_file", direct_peer))] if online else (),
                    )
                    hits.append((3, 2, 8, ("direct_back", None)))
                    if direct_page_error is not None:
                        put(4, 2, [("✗ " + direct_page_error, "error")], chat_width)
                    else:
                        rows = []
                        for timestamp, direction, _kind, peer, text in direct_page_rows or []:
                            sender = "you" if direction == "sent" else peer
                            append_card(
                                rows,
                                message_rows(
                                    display_time(timestamp),
                                    sender,
                                    text,
                                    None,
                                    chat_width,
                                    username,
                                ),
                                f"{display_time(timestamp)} · {sender}",
                                text,
                            )
                        if not rows:
                            append_plain(rows, [[("No messages yet — say hello below.", "muted")]])
                        direct_scroll = draw_rows(rows, 4, direct_scroll)
            elif session.display_filter == "groups" and current_group is None:
                header_bar(3, [("Groups", "title"), ("  click a name or press Enter", "muted")])
                if not group_names:
                    put(4, 2, [("No groups yet.", "muted")], chat_width)
                    put(5, 2, [("Create one: ", "muted"), ("/group create NAME USER…", "key")],
                        chat_width)
                start = max(0, group_selected - content_capacity + 1)
                for y, index in enumerate(
                    range(start, min(len(group_names), start + content_capacity)), 4
                ):
                    name = group_names[index]
                    chosen = index == group_selected
                    owner = group_owners.get(name)
                    detail = f"{len(groups[name])} members" + (
                        " · you own it" if owner == username
                        else f" · owner {safe_display(owner)}" if owner else ""
                    )
                    put(y, 2, spread_row(
                        [("› " if chosen else "  ", "accent"),
                         ("#" + name, "selected" if chosen else "text")],
                        [(detail, "muted")], chat_width), chat_width)
                    hits.append((y, 2, 2 + chat_width, ("command", f"/group open {name}")))
            else:
                body_start = 3
                if session.display_filter == "groups":
                    owner = group_owners.get(current_group)
                    buttons = []
                    if current_group in groups:
                        buttons.append(("[Send file]", "accent", ("send_group_file", current_group)))
                        if owner is not None:
                            buttons.append(
                                ("[Manage]", "accent", ("group_menu", current_group))
                                if owner == username
                                else ("[Leave]", "error", ("confirm_group", "leave", current_group))
                            )
                    header_bar(
                        3,
                        [("← Groups", "accent"), ("   ", ""), ("#" + current_group, "title"),
                         (f"  {len(groups.get(current_group, ()))} members", "muted")]
                        + ([("  · owner", "faint")] if owner == username else []),
                        buttons,
                    )
                    hits.append((3, 2, 10, ("group_back",)))
                    body_start = 4
                rows = []
                if session.display_filter == "groups" and group_page_rows is not None:
                    for timestamp, direction, _kind, _peer, text in group_page_rows:
                        if direction == "sent":
                            sender = "you"
                        else:
                            sender, _, text = text.partition(": ")
                        append_card(
                            rows,
                            message_rows(display_time(timestamp), sender, text, f"#{current_group}",
                                         chat_width, username),
                            f"{display_time(timestamp)} · {sender} · #{current_group}",
                            text,
                        )
                for stamp, message, kind, original_text in messages:
                    if kind == "welcome":
                        if session.display_filter == "all":
                            append_plain(rows, welcome_rows(chat_width, username))
                        continue
                    if (
                        session.display_filter != "all"
                        and notice_category(message) != session.display_filter
                        and not (session.display_filter == "groups" and kind in {"error", "file"})
                    ):
                        continue
                    group = chat_group(message)
                    if session.display_filter == "groups" and group_page_rows is not None and group is not None:
                        continue
                    if session.display_filter == "groups" and group is not None and group != current_group:
                        continue
                    sender = chat_sender(message)
                    if sender is not None:
                        text = message.split(":", 1)[1].lstrip()
                        channel = f"#{group}" if group else "DM" if notice_category(message) == "dm" else "All"
                        append_card(
                            rows,
                            message_rows(stamp, sender, text, channel, chat_width, username),
                            f"{stamp} · {sender} · {channel}",
                            original_text if original_text is not None else text,
                        )
                    else:
                        display = re.sub(r"^\[(?:file|presence|admin|display)\] ", "", message)
                        append_plain(rows, notice_rows(stamp, display, kind, chat_width))
                if not rows and session.display_filter == "files":
                    append_plain(rows, [[("No files shared yet. ", "muted"), ("F7", "key"),
                                         (" → choose a person → Send file.", "muted")]])
                if not rows and session.display_filter == "global":
                    append_plain(rows, [[("No messages for everyone yet.", "muted")]])
                if not rows and session.display_filter == "groups":
                    append_plain(rows, [[(f"No messages in #{current_group} yet.", "muted")]])
                scroll = draw_rows(rows, body_start, scroll)
                if scroll == 0:
                    unseen = 0
            if sidebar:
                separator = width - sidebar
                side_x, side_width = separator + 2, sidebar - 4
                write(2, separator, "┬", 1, "faint")
                for y in range(3, box_top):
                    write(y, separator, "│", 1, "faint")
                # Rows are (segments, [(start, end, action)]); actionable
                # sections come first so buttons never fall off the bottom.
                side = []

                def heading(label, count, style="heading"):
                    if side:
                        side.append(([], []))
                    side.append(([(label, style), (f"  {count}", "faint")], []))

                if dock_rows:
                    heading("TRANSFERS", len(dock_rows), "file")
                    for row in dock_rows[:3]:
                        arrow = "↓ " if row.get("direction") == "in" else "↑ "
                        name_width = cell_width(arrow + safe_display(row["name"]) + " · ")
                        side.append((
                            [(arrow, "file"), (safe_display(row["name"]), "text"),
                             (" · " + safe_display(row["peer"]), "muted")],
                            [(name_width, name_width + cell_width(row["peer"]),
                              ("person", "user", row["peer"]))]
                            if row["peer"] in users and valid_username(row["peer"]) else [],
                        ))
                        if row["status"] == "offer":
                            side.append((
                                [("  ", ""), ("[Accept]", "success"), (" ", ""), ("[Reject]", "error")],
                                [(2, 10, ("accept_file", row["id"])),
                                 (11, 19, ("command", f"/reject {row['id']}"))],
                            ))
                        else:
                            bar = transfer_bar(row, side_width - 2)
                            side.append(([("  ", "")] + (bar or [(transfer_summary(row), "muted")]), []))
                if role == "admin" and pending:
                    heading("PENDING", len(pending), "admin")
                    for name in pending:
                        valid = valid_username(name)
                        side.append((
                            [("◆ ", "admin"), (safe_display(name), "text")],
                            [(0, 2 + cell_width(name), ("person", "pending", name))] if valid else [],
                        ))
                        if valid and side_width >= 20:
                            side.append((
                                [("  ", ""), ("[Approve]", "success"), (" ", ""), ("[Reject]", "error")],
                                [(2, 11, ("command", f"/approve {name}")),
                                 (12, 20, ("command", f"/reject-user {name}"))],
                            ))
                heading("ONLINE", len(users), "heading")
                people = [
                    (
                        [("● ", "success"), (safe_display(name), "text")],
                        [(0, 2 + cell_width(name), ("person", "user", name))]
                        if valid_username(name) else [],
                    )
                    for name in users
                ] or [([("nobody else yet", "faint")], [])]
                group_rows = []
                if groups:
                    group_rows.append(([], []))
                    group_rows.append(([("GROUPS", "heading"), (f"  {len(groups)}", "faint")], []))
                    for name in sorted(groups):
                        group_rows.append((
                            spread_row([("#" + name, "accent" if name == current_group else "text")],
                                       [(str(len(groups[name])), "faint")], side_width),
                            [(0, side_width, ("command", f"/group open {name}"))],
                        ))
                available = box_top - 3
                room = available - len(side) - min(len(group_rows), 4)
                if len(people) > room:
                    hidden = len(people) - max(1, room - 1)
                    people = people[: max(1, room - 1)] + [
                        ([(f"+{hidden} more · ", "faint"), ("F7", "key")], [])
                    ]
                side += people + group_rows
                for y, (segments, actions) in enumerate(side[:available], 3):
                    put(y, side_x, segments, side_width)
                    for start_x, end_x, action in actions:
                        hits.append((y, side_x + start_x, side_x + end_x, action))
            for y, row in enumerate(dock_rows[:dock_height], dock_y):
                arrow = "↓ " if row.get("direction") == "in" else "↑ "
                if row["status"] == "offer":
                    buttons = "[Accept] [Reject]"
                    room = max(1, chat_width - cell_width(buttons) - 1)
                    label = fit_row([(arrow, "file"), (safe_display(row["peer"]), "text"),
                                     (" sends ", "muted"), (safe_display(row["name"]), "file")], room)
                    accept_x = 2 + cell_width(row_text(label)) + 1
                    reject_x = accept_x + cell_width("[Accept]") + 1
                    put(y, 2, label, room)
                    write(y, accept_x, "[Accept]", width - accept_x - 2, "success")
                    write(y, reject_x, "[Reject]", width - reject_x - 2, "error")
                    hits.append((y, accept_x, accept_x + 8, ("accept_file", row["id"])))
                    hits.append((y, reject_x, reject_x + 8, ("command", f"/reject {row['id']}")))
                else:
                    label = [(arrow, "file"), (fit_cells(safe_display(row["name"]), 24), "text"),
                             (" · " + safe_display(row["peer"]) + "  ", "muted")]
                    bar = transfer_bar(row, max(0, min(30, chat_width - cell_width(row_text(label)))))
                    put(y, 2, label + (bar or [(transfer_summary(row), "muted")]), chat_width)
            if popup_height:
                start = max(0, selection - popup_height + 1)
                column = min(
                    30,
                    max(cell_width(option.value + " " + option.usage) for option in options) + 3,
                )
                for row, index in enumerate(range(start, min(len(options), start + popup_height))):
                    option = options[index]
                    chosen = index == selection
                    usage = " " + option.usage if option.usage else ""
                    segments = [
                        ("› " if chosen else "  ", "accent"),
                        (option.value, "accent" if chosen else "text"),
                        (usage, "muted"),
                    ]
                    if chat_width >= 50:
                        gap = max(2, column - cell_width(option.value + usage))
                        segments += [(" " * gap, ""),
                                     (option.description, "text" if chosen else "muted")]
                        if option.admin:
                            segments.append(("  admin", "admin"))
                    put(popup_y + row, 2, segments, chat_width)
                    hits.append((popup_y + row, 2, 2 + chat_width, ("suggestion", index)))
            # The input box title always names where plain text will go.
            if composer.text.startswith("/"):
                target, border = [("command", "accent")], "accent"
            elif composer.text.startswith("@"):
                target, border = [("private message", "dm")], "dm"
            elif session.display_filter == "groups":
                target = (
                    [("to ", "muted"), ("#" + current_group, "success")]
                    if current_group else [("choose a group first", "muted")]
                )
                border = "faint"
            elif direct_peer is not None:
                target, border = [("private to ", "muted"), (direct_peer, "dm")], "faint"
            elif session.display_filter == "dm":
                target, border = [("open a conversation first", "muted")], "faint"
            else:
                target, border = [("to ", "muted"), ("everyone", "accent")], "faint"
            scrolled = (
                direct_scroll if direct_peer is not None
                else scroll if session.display_filter != "dm" else 0
            )
            status = (
                [(f"↓ {new_count} new message{'s' if new_count != 1 else ''}", "accent"),
                 (" · PgDn", "muted")]
                if new_count
                else [("scrolled up · PgDn", "muted")]
                if scrolled
                else []
            )
            box(box_top, 1, 3, width - 2, border, target, status, server_note)
            write(input_y, 3, "›", 1, "accent")
            # Keep the edit cursor visible, including wide CJK characters.
            room = width - 8
            offset = 0
            while cell_width(composer.text[offset : composer.cursor]) > room - 1:
                offset += 1
            visible = composer.text[offset:]
            placeholder = (
                f"Message #{current_group}" if session.display_filter == "groups" and current_group
                else "Choose a group above" if session.display_filter == "groups"
                else f"Message {direct_peer} privately, or / for commands"
                if direct_peer is not None
                else "Open a conversation, or / for commands"
                if session.display_filter == "dm"
                else "Message everyone, or / for commands"
            )
            write(input_y, 5, visible or placeholder, room, "text" if visible else "faint")
            guidance = composer_guidance(composer.text)
            if composer.text.startswith("/reply "):
                guidance = (
                    f"Reply to {reply_target}"
                    if reply_target
                    else "Choose a recipient with /msg USER TEXT"
                )
            ghost = (
                "path/to/file"
                if guidance and guidance.startswith("File:")
                else "message text"
                if guidance and guidance.startswith("Message:")
                else ""
            )
            if ghost and composer.cursor == len(composer.text):
                write(input_y, 5 + cell_width(visible), ghost,
                      max(0, room - cell_width(visible)), "faint")
            entries = []
            if menu is not None:
                kind, target = menu[0], menu[1]
                if kind == "people":
                    entries = [
                        (f"● {name}", ("open_user", name)) for name in users if valid_username(name)
                    ]
                    if role == "admin":
                        entries += [
                            (f"◆ {name} (pending)", ("open_pending", name))
                            for name in pending
                            if valid_username(name)
                        ]
                elif kind == "user" and target in users and valid_username(target):
                    entries = [
                        ("Direct message", ("direct_peer", target)),
                        ("Send file", ("send_file", target)),
                    ]
                elif kind == "group" and target in groups and group_owners.get(target) == username:
                    entries = [
                        ("Add member...", ("group_add", target)),
                        ("Delete group...", ("confirm_group", "delete", target)),
                    ]
                    entries += [
                        (f"Kick {member}", ("command", f"/group kick {target} {member}"))
                        for member in groups[target] if member != username
                    ]
                elif kind == "group_confirm":
                    verb, name = target
                    owner = group_owners.get(name)
                    if name in groups and owner is not None and (
                        (verb == "delete" and owner == username) or (verb == "leave" and owner != username)
                    ):
                        entries = [
                            (f"Confirm {verb} #{name}", ("command", f"/group {verb} {name}")),
                            ("Cancel", ("close_menu",)),
                        ]
                elif (
                    kind == "pending"
                    and role == "admin"
                    and target in pending
                    and valid_username(target)
                ):
                    entries = [
                        ("Approve", ("command", f"/approve {target}")),
                        ("Reject", ("command", f"/reject-user {target}")),
                    ]
                if not entries:
                    menu = None
            if menu is not None:
                menu[2] = min(menu[2], len(entries) - 1)
                title = (
                    "Confirm group action" if menu[0] == "group_confirm"
                    else "People"
                    if menu[0] == "people"
                    else f"Pending: {menu[1]}"
                    if menu[0] == "pending"
                    else f"#{menu[1]}"
                    if menu[0] == "group"
                    else menu[1]
                )
                menu_visible = min(len(entries), max(1, box_top - 5))
                menu_start = min(
                    max(0, menu[2] - menu_visible + 1),
                    max(0, len(entries) - menu_visible),
                )
                if len(entries) > menu_visible:
                    title = f"{title} {menu[2] + 1}/{len(entries)}"
                menu_width = min(
                    width - 4,
                    max([cell_width(title) + 6] + [cell_width(label) + 7 for label, _ in entries]),
                )
                menu_height = menu_visible + 2
                anchor_x, anchor_y = menu[3], menu[4]
                if anchor_x is None:
                    menu_x = max(2, (width - menu_width) // 2)
                    menu_y = max(3, (box_top - menu_height) // 2)
                else:
                    menu_x = min(max(2, anchor_x), width - menu_width - 2)
                    menu_y = min(max(3, anchor_y), box_top - menu_height)
                if menu_width >= 12 and menu_y >= 3:
                    box(menu_y, menu_x, menu_height, menu_width, "accent", [(title, "title")])
                    for menu_offset, (label, _) in enumerate(
                        entries[menu_start : menu_start + menu_visible]
                    ):
                        row = menu_y + 1 + menu_offset
                        index = menu_start + menu_offset
                        chosen = index == menu[2]
                        style = (
                            "highlight" if chosen
                            else "error" if label.startswith(DESTRUCTIVE) else "text"
                        )
                        if chosen:
                            write(row, menu_x + 1, " " * (menu_width - 2), menu_width - 2, style)
                        put(row, menu_x + 2, [("› " if chosen else "  ", style), (label, style)],
                            menu_width - 4)
                        hits.append((row, menu_x, menu_x + menu_width, ("menu_item", index)))
                else:
                    menu = None
            loss_hint = [(loss, "muted"), ("  ", ""), ("F4", "key"), (" test", "muted")]
            if closed:
                hint = [("Disconnected — press any key to exit", "error")]
            elif direct_flash is not None and monotonic() < direct_flash[2]:
                glyph = "✗ " if direct_flash[1] == "error" else ""
                hint = [(glyph + direct_flash[0], direct_flash[1])]
            elif menu is not None:
                hint = hint_row([("↑↓", "choose"), ("Enter", "select"), ("Esc", "close")])
            elif options:
                hint = hint_row([("↑↓", "choose"), ("Tab", "complete"), ("Enter", "use"),
                                 ("Esc", "dismiss")])
            elif guidance:
                hint = [(guidance, "muted")]
            elif direct_peer is not None:
                hint = hint_row([("Enter", "send"), ("Esc", "inbox"), ("F9", "copy"),
                                 ("PgUp", "scroll"), ("F1", "help")])
            elif session.display_filter == "dm":
                hint = hint_row([("↑↓", "choose"), ("Enter", "open"), ("F7", "people"),
                                 ("F1", "help")])
            elif session.display_filter == "groups" and current_group is None:
                hint = hint_row([("↑↓", "choose"), ("Enter", "open"), ("Tab", "next tab")])
            else:
                hint = hint_row([("Enter", "send"), ("/", "commands"), ("Tab", "tabs"),
                                 ("F7", "people"), ("F8", "history"), ("F1", "help")])
            for right in (loss_hint, [(loss, "muted")], []):
                if cell_width(row_text(right)) <= width - 4 - min(16, cell_width(row_text(hint))):
                    break
            if hint and hint[0][1] != "key":
                right = []  # Errors and notices need the whole footer.
            footer(hint, right)
            try:
                window.move(input_y, 5 + cell_width(composer.text[offset : composer.cursor]))
                window.refresh()
                key = window.get_wch()
            except curses.error:
                continue
            if closed:
                return
            before = composer.text
            if key in ("\x03", "\x04"):
                return
            if key == getattr(curses, "KEY_RESIZE", -999):
                menu = None  # Hitboxes and positions are rebuilt from the new size.
                continue
            if menu is not None:
                if key == curses.KEY_MOUSE:
                    try:
                        _, mouse_x, mouse_y, _, bstate = curses.getmouse()
                    except curses.error:
                        pass
                    else:
                        if bstate & getattr(curses, "BUTTON1_CLICKED", 0):
                            hit = hit_at(mouse_x, mouse_y)
                            if hit and hit[0] == "menu_item" and 0 <= hit[1] < len(entries):
                                if not perform(entries[hit[1]][1]):
                                    return
                            else:
                                menu = None
                elif key in ("\x1b", "\x03"):
                    menu = None
                elif key in (curses.KEY_UP, curses.KEY_DOWN):
                    step = -1 if key == curses.KEY_UP else 1
                    menu[2] = (menu[2] + step) % max(1, len(entries))
                elif key in ("\n", "\r", curses.KEY_ENTER) and entries:
                    if not perform(entries[menu[2]][1]):
                        return
                continue
            if key == "\x1b":
                dismissed = True
                alternate = None
                window.timeout(30)
                try:
                    alternate = window.get_wch()
                    if isinstance(alternate, str) and alternate in "12345":
                        select_filter(FILTERS[int(alternate) - 1])
                        scroll = 0
                    elif isinstance(alternate, str) and alternate.isprintable():
                        composer.insert(alternate)
                except curses.error:
                    pass
                finally:
                    window.timeout(100)
                if alternate is None and direct_peer is not None:
                    back_to_inbox()
                elif alternate is None and session.display_filter == "groups" and current_group is not None:
                    session.current_group = None
            elif key == getattr(curses, "KEY_F1", -1):
                show_help = True
                help_scroll = 0
            elif key == getattr(curses, "KEY_F9", -9):
                copy_picker = CopyPicker(copy_entries)
            elif key == getattr(curses, "KEY_F8", -8):
                try:
                    history_browser = HistoryBrowser(session)
                except (ValueError, OSError, sqlite3.Error) as error:
                    add(str(error), "error")
            elif key == getattr(curses, "KEY_F2", -2):
                sidebar_enabled = not sidebar_enabled
            elif key == getattr(curses, "KEY_F3", -3):
                cycle_filter(1)
            elif key == getattr(curses, "KEY_F4", -4):
                start_probe()
            elif key == getattr(curses, "KEY_F5", -5):
                session.execute(parse_command("/transfers"))
            elif key == getattr(curses, "KEY_F6", -6):
                offers = [row for row in transfers if row["status"] == "offer"]
                if not run_command(f"/accept {offers[0]['id']}" if offers else "/accept"):
                    return
            elif key == getattr(curses, "KEY_F7", -7):
                # Keyboard route to the same menu a right-click opens.
                if users or (role == "admin" and pending):
                    menu = ["people", None, 0, None, None]
                else:
                    add("No one online or awaiting approval yet.", "muted")
            elif key == "\x0b":
                composer.replace(composer.text[: composer.cursor])
            elif key == "\x0c":
                session.execute(parse_command("/clear"))
            elif key == "\t" and options:
                composer.replace(options[selection].value + " ")
            elif key == "\t" and not composer.text:
                cycle_filter(1)
            elif key == getattr(curses, "KEY_BTAB", -998) and not composer.text:
                cycle_filter(-1)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if options and composer.text != options[selection].value:
                    composer.replace(options[selection].value + " ")
                elif (
                    session.display_filter == "groups" and current_group is None
                    and composer.text and not composer.text.startswith(("/", "@"))
                ):
                    message = "Choose a group first; your draft is kept."
                    add(message, "error")
                elif (
                    session.display_filter == "groups" and current_group is None
                    and not composer.text and group_names
                ):
                    run_command(f"/group open {group_names[group_selected]}")
                elif (
                    session.display_filter == "dm"
                    and direct_peer is None
                    and composer.text
                    and not composer.text.startswith(("/", "@"))
                ):
                    message = "Open a conversation first; plain text is never sent globally."
                    add(message, "error")
                    direct_flash = (message, "error", monotonic() + 10)
                elif (
                    session.display_filter == "dm"
                    and direct_peer is None
                    and not composer.text
                    and direct_conversations
                ):
                    open_direct(direct_conversations[direct_selected][0])
                else:
                    line = composer.take()
                    if line:
                        if line.split(maxsplit=1)[0].lower() == "/theme":
                            words = line.split()[1:]
                            choice = (
                                words[0].lower() if words
                                else "light" if theme == "dark" else "dark"
                            )
                            if len(words) > 1 or choice not in THEMES:
                                add("Usage: /theme [dark|light]", "error")
                            else:
                                theme = choice
                                styles.clear()
                                styles.update(apply_theme(curses, window, theme))
                                window.clearok(True)
                                add(
                                    f"Switched to the {theme} theme."
                                    if save_theme(theme)
                                    else f"Switched to the {theme} theme for this session "
                                    "(could not save the preference).",
                                    "success",
                                )
                        elif line.strip().lower() == "/help":
                            show_help = True
                            help_scroll = 0
                        elif line.split(maxsplit=1)[0].lower() == "/history":
                            try:
                                command = parse_command(line)
                                history_browser = HistoryBrowser(session, command.args)
                            except (ValueError, OSError, sqlite3.Error) as error:
                                add(str(error), "error")
                        else:
                            if not line.startswith(("/", "@")):
                                line = (
                                    f"/gmsg {current_group} {line}"
                                    if session.display_filter == "groups"
                                    else
                                    f"/msg {direct_peer} {line}"
                                    if direct_peer is not None
                                    else "/all " + line
                                )
                            try:
                                if not session.execute(parse_command(line)):
                                    return
                                if session.display_filter == "dm":
                                    direct_dirty = True
                                    direct_flash = None
                            except ValueError as error:
                                add(str(error), "error")
                    scroll = 0
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                direction = -1 if key == curses.KEY_UP else 1
                if options:
                    selection = (selection + direction) % len(options)
                elif session.display_filter == "groups" and current_group is None:
                    group_selected = max(0, min(len(group_names) - 1, group_selected + direction))
                elif session.display_filter == "dm":
                    if direct_peer is None and direct_conversations:
                        direct_selected = max(
                            0, min(len(direct_conversations) - 1, direct_selected + direction)
                        )
                    elif direct_peer is not None:
                        direct_scroll = max(0, direct_scroll - direction)
                else:
                    composer.recall(direction)
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                if composer.cursor:
                    composer.text = (
                        composer.text[: composer.cursor - 1] + composer.text[composer.cursor :]
                    )
                    composer.cursor -= 1
            elif key == curses.KEY_DC:
                composer.text = (
                    composer.text[: composer.cursor] + composer.text[composer.cursor + 1 :]
                )
            elif key == curses.KEY_LEFT:
                composer.cursor = max(0, composer.cursor - 1)
            elif key == curses.KEY_RIGHT:
                composer.cursor = min(len(composer.text), composer.cursor + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                composer.cursor = 0
            elif key in (curses.KEY_END, "\x05"):
                composer.cursor = len(composer.text)
            elif key == "\x15":
                composer.replace("")
            elif key == "\x17":
                composer.delete_word()
            elif key == curses.KEY_PPAGE:
                if direct_peer is not None:
                    direct_scroll += content_capacity
                elif session.display_filter == "groups" and current_group is None:
                    group_selected = max(0, group_selected - content_capacity)
                else:
                    scroll += content_capacity
            elif key == curses.KEY_NPAGE:
                if direct_peer is not None:
                    direct_scroll = max(0, direct_scroll - content_capacity)
                elif session.display_filter == "groups" and current_group is None:
                    group_selected = min(len(group_names) - 1, group_selected + content_capacity)
                else:
                    scroll = max(0, scroll - content_capacity)
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, bstate = curses.getmouse()
                except curses.error:
                    pass
                else:
                    right = bool(
                        bstate
                        & (
                            getattr(curses, "BUTTON3_CLICKED", 0)
                            | getattr(curses, "BUTTON3_PRESSED", 0)
                        )
                    )
                    if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                        if direct_peer is not None:
                            direct_scroll += 3  # Wheel up: older transcript.
                        elif session.display_filter == "groups" and current_group is None:
                            group_selected = max(0, group_selected - 3)
                        else:
                            scroll += 3  # Wheel up: older messages.
                    elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                        if direct_peer is not None:
                            direct_scroll = max(0, direct_scroll - 3)
                        elif session.display_filter == "groups" and current_group is None:
                            group_selected = min(len(group_names) - 1, group_selected + 3)
                        else:
                            scroll = max(0, scroll - 3)  # Wheel down: back to live.
                    elif right:
                        hit = hit_at(mouse_x, mouse_y)
                        if hit and hit[0] == "copy_message":
                            copy_picker = CopyPicker(copy_entries, hit[1])
                        elif hit and hit[0] == "person":
                            anchor_x = (
                                width - sidebar
                                if sidebar and mouse_x >= width - sidebar
                                else mouse_x
                            )
                            menu = [hit[1], hit[2], 0, anchor_x, mouse_y]
                        else:
                            menu = None
                    elif bstate & getattr(curses, "BUTTON1_CLICKED", 0):
                        hit = hit_at(mouse_x, mouse_y)
                        if menu is not None:
                            if hit and hit[0] == "menu_item" and 0 <= hit[1] < len(entries):
                                if not perform(entries[hit[1]][1]):
                                    return
                            else:
                                menu = None
                        elif hit:
                            if hit[0] == "copy_message":
                                copy_picker = CopyPicker(copy_entries, hit[1])
                            elif hit[0] == "direct_peer":
                                open_direct(hit[1])
                            elif hit[0] == "direct_back":
                                back_to_inbox()
                            elif hit[0] == "person":
                                # Left click is the fallback opener when the
                                # terminal cannot report right-clicks.
                                anchor_x = (
                                    width - sidebar
                                    if sidebar and mouse_x >= width - sidebar
                                    else mouse_x
                                )
                                menu = [hit[1], hit[2], 0, anchor_x, mouse_y]
                            elif hit[0] == "filter":
                                select_filter(hit[1])
                                scroll = 0
                            elif hit[0] in {
                                "command", "accept_file", "send_file", "send_group_file",
                                "group_menu", "confirm_group", "group_back",
                            }:
                                if not perform(hit):
                                    return
                            elif hit[0] == "suggestion" and 0 <= hit[1] < len(options):
                                composer.replace(options[hit[1]].value + " ")
            elif isinstance(key, str) and key.isprintable():
                composer.insert(key)
            if composer.text != before:
                selection, dismissed = 0, False

    try:
        curses.wrapper(screen)
    except KeyboardInterrupt:
        pass
    finally:
        session.output = original_output
        session.close()
