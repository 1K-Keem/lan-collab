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
from lancollab.client.cli import Command, _DETAILED_HELP, parse_command, progress_bar
from lancollab.client.history import local_time
from lancollab.client.branding import banner_lines


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
    Suggestion("/filter", "CATEGORY", "all, global, dm or files"),
    Suggestion("/all", "TEXT", "Message everyone online"),
    Suggestion("/msg", "USER TEXT", "Send a private message"),
    Suggestion("/send", "USER PATH", "Offer a file to someone"),
    Suggestion("/users", "", "See who is online"),
    Suggestion("/history", "[USER|all|show ID]", "Browse your saved login sessions"),
    Suggestion("/accept", "ID", "Accept an incoming file"),
    Suggestion("/reject", "ID", "Decline a file offer"),
    Suggestion("/cancel", "ID", "Cancel a file transfer"),
    Suggestion("/status", "", "Connection and network status"),
    Suggestion("/net", "", "Check network quality"),
    Suggestion("/pending", "", "List accounts awaiting approval", True),
    Suggestion("/approve", "USER", "Approve a new member", True),
    Suggestion("/reject-user", "USER", "Reject a pending account", True),
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
        if command == "/filter":
            return [
                Suggestion(f"/filter {name}", "", "Message category")
                for name in FILTERS
                if name.startswith(argument)
            ]
    if command in {"/msg", "/send", "/history"} and " " not in argument:
        candidates = [(name, "Online user") for name in sorted(users)]
        if command == "/history":
            candidates.append(("all", "All sessions"))
        return [
            Suggestion(f"{command} {name}", "", description)
            for name, description in candidates
            if name.startswith(argument)
        ]
    return []


FILTERS = ("all", "global", "dm", "files")


def notice_category(message):
    if message.startswith(("[chat] [CHAT_PRIVATE]", "[CHAT_PRIVATE]")):
        return "dm"
    if message.startswith(("[chat] [CHAT_GLOBAL]", "[CHAT_GLOBAL]")):
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
    match = re.match(r"^(?:\[chat\] )?\[(?:CHAT_GLOBAL|CHAT_PRIVATE)\] ([^:]+):", message)
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
        raise RuntimeError("Picker needs tkinter; use /accept ID or /send USER PATH instead.") from error
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        if folder:
            return filedialog.askdirectory(parent=root, title="Choose where to save the file", mustexist=True)
        return filedialog.askopenfilename(parent=root, title="Choose a file to send")
    except tk.TclError as error:
        raise RuntimeError("Cannot open picker; use /accept ID or /send USER PATH instead.") from error
    finally:
        if root is not None:
            root.destroy()

def composer_guidance(text):
    """Hint for a bare /msg or /send template, including path guidance."""
    head, _, rest = text.partition(" ")
    argument = rest.strip()
    if argument and " " not in argument:
        if head == "/send":
            return "File: type or paste a path (quote spaces), then Enter"
        if head == "/msg":
            return "Message: type your private message, then Enter"
    return None

SENDER_STYLES = ("sender_a", "sender_b", "sender_c", "sender_d", "sender_e")


def sender_style_name(sender, username):
    """Own messages are bold accent; others get a stable color per sender."""
    if sender is None:
        return None
    if sender == "you" or sender.startswith("you ->"):
        return "accent"
    return SENDER_STYLES[sum(sender.encode("utf-8")) % len(SENDER_STYLES)]


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


def _palette(curses):
    styles = {
        name: 0
        for name in (
            "accent",
            "success",
            "muted",
            "error",
            "file",
            "selected",
            "dm",
            "admin",
            *SENDER_STYLES,
        )
    }
    styles["accent"] = curses.A_BOLD
    styles["muted"] = curses.A_DIM
    styles["selected"] = curses.A_REVERSE
    styles["dm"] = curses.A_BOLD
    try:
        if "NO_COLOR" not in os.environ and curses.has_colors():
            curses.start_color()
            background = curses.COLOR_BLACK
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                pass
            for number, (name, color) in enumerate(
                (
                    ("accent", curses.COLOR_CYAN),
                    ("success", curses.COLOR_GREEN),
                    ("muted", curses.COLOR_WHITE),
                    ("error", curses.COLOR_RED),
                    ("file", curses.COLOR_YELLOW),
                    ("sender_a", curses.COLOR_MAGENTA),
                    ("sender_b", curses.COLOR_BLUE),
                    ("sender_c", curses.COLOR_YELLOW),
                    ("sender_d", curses.COLOR_GREEN),
                    ("sender_e", curses.COLOR_CYAN),
                    ("dm", curses.COLOR_MAGENTA),
                    ("admin", curses.COLOR_BLUE),
                ),
                1,
            ):
                curses.init_pair(number, color, background)
                styles[name] |= curses.color_pair(number)
            styles["selected"] |= styles["accent"]
    except curses.error:
        pass  # Monochrome terminals retain emphasis and selection.
    return styles


def help_lines(width):
    """Full-screen help content as (text, style) rows, wrapped to width.

    Built from the team's own _DETAILED_HELP reference, so this screen and
    /help in the plain CLI can never drift apart. F1 or /help opens this
    view; Ctrl+C (or Esc) returns to the chat.
    """
    rows = [("LAN COLLAB — Help", "accent"), ("", "muted")]
    for line in _DETAILED_HELP:
        if not line:
            rows.append(("", "muted"))
        elif line.startswith("  "):
            for part in wrap_cells(line, max(10, width)):
                rows.append((part, "muted"))
        elif line.startswith("/"):
            for part in wrap_cells(line, max(10, width)):
                rows.append((part, "success"))
        elif line.isupper() or line.startswith("ADMIN ONLY"):
            for part in wrap_cells(line, max(10, width)):
                rows.append((part, "accent"))
        else:
            for part in wrap_cells(line, max(10, width)):
                rows.append((part, "muted"))
    rows.append(("", "muted"))
    rows.append(("Ctrl+C / Esc — back to chat", "accent"))
    return rows


def run_tui(session):
    import curses

    notices = Queue()
    original_output = session.output
    session.output = notices.put

    def screen(window):
        styles = _palette(curses)
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
        direct_selected = direct_scroll = 0
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
        history_records = None
        history_selected = history_scroll = 0
        history_lines = None
        history_wrapped_width = None
        history_rows = []
        sidebar_enabled = True
        # Transient context menu: kind, target, index, anchor. Rebuilt and
        # validated on every frame so resizes and disappearing people are safe.
        menu = None
        clear_generation = session.clear_generation
        next_pending_refresh = monotonic() + 10
        probe_worker = None

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
            # Only real events get a time prefix. Help text, history rows
            # (which carry their own timestamp) and plain notices do not.
            messages.append(
                (datetime.now().strftime("%H:%M") if stamp else "", safe_display(text), kind)
            )

        for line in banner_lines(window.getmaxyx()[1] - 10):
            messages.append(("", line, "accent"))
        add("Welcome to your study space.", "accent")
        add("Write a message to everyone, or type / to explore commands.")
        add("Try /msg for a private chat, /send to share a file, or /history to catch up.")

        def write(y, x, text, width, style="muted"):
            if width <= 0:
                return
            try:
                window.addstr(y, x, fit_cells(text, width), styles.get(style, 0))
            except curses.error:
                pass  # Resizes and the bottom-right cell can interrupt a draw.

        def run_command(line):
            """Dispatch through the same backend as typed commands; False means quit."""
            nonlocal direct_flash
            try:
                if not session.execute(parse_command(line)):
                    return False
            except ValueError as error:
                message = safe_display(str(error))
                add(message, "error")
                if session.display_filter == "dm":
                    direct_flash = (message, "error", monotonic() + 10)
            return True

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
            nonlocal context_key, selection, dismissed
            if key == context_key:
                return
            drafts[context_key] = composer.text
            context_key = key
            composer.replace(drafts.get(key, ""))
            selection, dismissed = 0, False

        def reset_direct_page():
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll
            direct_peer = None
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = 0

        def open_direct(peer):
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll, last_filter, direct_dirty
            nonlocal direct_flash
            if not valid_username(peer):
                return False
            switch_context(f"direct:{peer}")
            direct_peer = peer
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = 0
            direct_dirty = True
            direct_flash = None
            session.display_filter = "dm"
            last_filter = "dm"
            return True

        def back_to_inbox():
            nonlocal direct_peer, direct_page_rows, direct_page_revision
            nonlocal direct_page_error, direct_scroll
            if direct_peer is None:
                return
            switch_context("dm-inbox")
            direct_peer = None
            direct_page_rows = None
            direct_page_revision = None
            direct_page_error = None
            direct_scroll = 0

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
                    direct_conversations = [
                        tuple(row) for row in session.direct_conversations()
                    ]
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
            if action[0] == "direct_peer":
                menu = None
                return open_direct(action[1])
            if action[0] == "direct_back":
                menu = None
                back_to_inbox()
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
            if action[0] == "send_file":
                menu = None
                try:
                    path = native_picker()
                    if path:
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

        while session.client.state is not ClientState.CLOSED:
            if monotonic() >= next_pending_refresh:
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
                    add(notice, kind, stamp=notice.startswith("["))
                    if kind == "dm":
                        direct_dirty = True
                    if session.display_filter == "dm" and not notice.startswith("[presence]"):
                        # Errors and send results stay visible even though the
                        # Direct view replaces ordinary scrollback.
                        direct_flash = (
                            safe_display(notice),
                            "error" if kind == "error" else kind,
                            monotonic() + 10,
                        )
                    if scroll:
                        unseen += 1
                except Empty:
                    break
            if session.display_filter != last_filter:
                # Leaving Direct must never keep a peer route (or its draft)
                # active; drafts are stored per peer and restored on return.
                reset_direct_page()
                last_filter = session.display_filter
                menu = None
                direct_flash = None
                scroll = 0
                switch_context("dm-inbox" if last_filter == "dm" else "main")
            if history_records is not None:
                height, width = window.getmaxyx()
                window.erase()
                visible = max(1, height - 5)
                write(0, 2, "LAN COLLAB — Session history · read only", width - 4, "accent")
                if history_lines is None:
                    write(1, 2, "Your login sessions · newest first", width - 4)
                    start = max(0, history_selected - visible + 1)
                    if not history_records:
                        write(3, 2, "No saved sessions.", width - 4)
                    for y, index in enumerate(
                        range(start, min(len(history_records), start + visible)), 3
                    ):
                        record = history_records[index]
                        label = session.history_label(record)
                        write(
                            y,
                            2,
                            ("› " if index == history_selected else "  ") + label,
                            width - 4,
                            "selected" if index == history_selected else "muted",
                        )
                    hint = "↑↓ choose · Enter view · Esc / Ctrl+C back to chat"
                else:
                    if history_wrapped_width != width:
                        history_rows = [
                            part
                            for line in history_lines
                            for part in wrap_cells(line, max(1, width - 4))
                        ]
                        history_wrapped_width = width
                    history_scroll = min(history_scroll, max(0, len(history_rows) - visible))
                    for y, line in enumerate(
                        history_rows[history_scroll : history_scroll + visible], 2
                    ):
                        write(y, 2, line, width - 4)
                    hint = "↑↓ / PgUp/PgDn scroll · Esc / Ctrl+C back to sessions"
                write(height - 1, 2, hint, width - 4)
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
                if key in ("\x1b", "\x03"):
                    if history_lines is not None:
                        history_lines = None
                        history_scroll = 0
                    else:
                        history_records = None
                        try:
                            curses.curs_set(1)
                        except curses.error:
                            pass
                elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE):
                    step = (
                        -1
                        if key == curses.KEY_UP
                        else 1
                        if key == curses.KEY_DOWN
                        else (-visible if key == curses.KEY_PPAGE else visible)
                    )
                    if history_lines is not None:
                        history_scroll = max(0, history_scroll + step)
                    else:
                        history_selected = max(
                            0, min(len(history_records) - 1, history_selected + step)
                        )
                elif (
                    key in ("\n", "\r", curses.KEY_ENTER)
                    and history_lines is None
                    and history_records
                ):
                    history_wrapped_width = None
                    try:
                        record, transcript = session.history_transcript(
                            history_records[history_selected].id or "legacy"
                        )
                        history_lines = [safe_display(session.history_label(record)), ""]
                        history_lines += [session.history_message(row) for row in transcript]
                        if not transcript:
                            history_lines.append("No saved messages in this session.")
                    except (ValueError, OSError, sqlite3.Error) as error:
                        history_lines = [safe_display(str(error))]
                # There is deliberately no composer or command dispatch in this view.
                continue
            if show_help:
                height, width = window.getmaxyx()
                window.erase()
                rows = help_lines(max(10, width - 4))
                visible = max(1, height - 4)
                help_scroll = min(help_scroll, max(0, len(rows) - visible))
                for y, (line, style) in enumerate(rows[help_scroll : help_scroll + visible], 1):
                    write(y, 2, line, width - 4, style)
                write(
                    height - 1,
                    2,
                    "↑↓ / PgUp/PgDn scroll · Ctrl+C / Esc back to chat",
                    width - 4,
                    "muted",
                )
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
                continue
            client = session.client
            with client._lock:
                username, role = client.username, client.role
                users = sorted(name for name in client.online_users if name != username)
                pending = [item["username"] for item in client.pending_users]
                reply_target = client.reply_target
                tcp_rtt = client.last_tcp_rtt
            transfers = client.files.snapshot()
            options = (
                suggestions(composer.text, users, role, pending, transfers) if not dismissed else []
            )
            selection = min(selection, max(0, len(options) - 1))
            height, width = window.getmaxyx()
            window.erase()
            if height < 12 or width < 40:
                write(0, 0, "Enlarge terminal to 40 x 12; Ctrl+C exits", width - 1)
                window.refresh()
                try:
                    if window.get_wch() in ("\x03", "\x04"):
                        return
                except curses.error:
                    pass
                continue
            sidebar = 24 if sidebar_enabled and width >= 76 else 0
            chat_width = width - sidebar - 4
            popup_height = min(6, len(options), max(0, height - 12))
            input_y = height - 3
            popup_y = input_y - popup_height - 1
            dock_height = min(3, len(transfers), max(0, height - popup_height - 10))
            dock_y = popup_y - dock_height
            capacity = max(1, dock_y - 5)
            write(0, 2, "LAN COLLAB", 12, "accent")
            write(0, 16, f"{username} / {role} | reply: {reply_target or '—'}", width - 18)
            if scroll:
                status = f"Scrollback · {unseen} new ↓" if unseen else "Scrollback"
            else:
                status = "Live conversation"
            udp_rtt = session.udp.quality.last_rtt_ms if session.udp is not None else None

            def latency(value):
                return "—" if value is None else f"{value:.0f}ms"

            write(
                1,
                2,
                f"TLS ✓ | {len(users)} online | TCP {latency(tcp_rtt)} UDP {latency(udp_rtt)} | {status}",
                width - 4,
                "success",
            )
            divider = "─" * (width - 4)
            if sidebar:
                split = width - sidebar - 3
                divider = divider[:split] + "┬" + divider[split + 1 :]
            write(2, 2, divider, width - 4)
            tabs = (
                ("all", "1:All"),
                ("global", "2:Global"),
                ("dm", "3:Direct"),
                ("files", "4:Files"),
            )
            hits = []
            tab_x = 2
            for category, label in tabs:
                write(
                    3,
                    tab_x,
                    label,
                    len(label),
                    "selected" if session.display_filter == category else "muted",
                )
                hits.append((3, tab_x, tab_x + len(label), ("filter", category)))
                tab_x += len(label) + 2
            if session.display_filter == "dm":
                refresh_direct()
                if direct_peer is None:
                    write(4, 2, "Direct conversations · newest first", width - 4, "accent")
                    body_start = 5
                    if not direct_conversations:
                        write(5, 2, "No private conversations yet. Open one from ONLINE.", width - 4)
                    else:
                        visible = max(1, capacity)
                        start = max(
                            0,
                            min(
                                direct_selected - visible + 1,
                                len(direct_conversations) - visible,
                            ),
                        )
                        for y, index in enumerate(
                            range(start, min(len(direct_conversations), start + visible)),
                            body_start,
                        ):
                            peer, timestamp, text = direct_conversations[index]
                            label = (
                                ("› " if index == direct_selected else "  ")
                                + safe_display(peer)
                                + "  "
                                + local_time(timestamp)
                                + "  "
                                + safe_display(text)
                            )
                            write(
                                y,
                                2,
                                fit_cells(label, chat_width),
                                chat_width,
                                "selected" if index == direct_selected else "muted",
                            )
                            hits.append((y, 2, width - 2, ("direct_peer", peer)))
                else:
                    online = direct_peer in users
                    write(
                        4,
                        2,
                        f"[Back] ← {direct_peer} · "
                        + ("online" if online else "offline · new messages stored"),
                        chat_width,
                        "accent" if online else "muted",
                    )
                    hits.append((4, 2, 2 + len("[Back]"), ("direct_back", None)))
                    body_start = 5
                    if direct_page_error is not None:
                        write(body_start, 2, direct_page_error, chat_width, "error")
                    else:
                        rows = []
                        for timestamp, direction, _kind, peer, text in direct_page_rows or []:
                            sender = "you" if direction == "sent" else safe_display(peer)
                            prefix = f"{local_time(timestamp)}  {sender}: "
                            style = "accent" if direction == "sent" else "dm"
                            for pre, line in wrap_prefixed(
                                prefix, safe_display(text), chat_width
                            ):
                                rows.append((pre, line, style))
                        if not rows:
                            rows = [("", "No private messages in this conversation yet.", "muted")]
                        direct_scroll = min(direct_scroll, max(0, len(rows) - capacity))
                        end = len(rows) - direct_scroll
                        for y, (pre, line, style) in enumerate(
                            rows[max(0, end - capacity) : end], body_start
                        ):
                            if pre:
                                write(y, 2, pre, chat_width, "muted")
                            write(
                                y,
                                2 + cell_width(pre),
                                line,
                                chat_width - cell_width(pre),
                                style,
                            )
            else:
                rows = []
                for stamp, message, kind in messages:
                    if (
                        session.display_filter != "all"
                        and notice_category(message) != session.display_filter
                    ):
                        continue
                    prefix = f"{stamp}  " if stamp else ""
                    style = kind
                    for pre, line in wrap_prefixed(prefix, message, chat_width):
                        rows.append((pre, line, style))
                scroll = min(scroll, max(0, len(rows) - capacity))
                if scroll == 0:
                    unseen = 0
                end = len(rows) - scroll
                for y, (pre, line, style) in enumerate(
                    rows[max(0, end - capacity) : end], 4
                ):
                    if pre:
                        write(y, 2, pre, chat_width, "muted")
                    write(
                        y,
                        2 + cell_width(pre),
                        line,
                        chat_width - cell_width(pre),
                        style,
                    )
            # Offers sort first so active transfers cannot push Approve/Accept
            # buttons out of the visible dock.
            dock_rows = sorted(transfers, key=lambda row: row["status"] != "offer")
            if sidebar:
                x = width - sidebar
                for y in range(3, dock_y):
                    write(y, x - 1, "│", 1)
                sidebar_rows = [("ONLINE", "accent", None, None)]
                sidebar_rows += [
                    ("● " + name, "success", "user", name)
                    for name in users[: max(1, (dock_y - 12) // 2)]
                ]
                sidebar_rows += [("ACTIVE TRANSFERS", "accent", None, None)]
                for row in dock_rows[:2]:
                    sidebar_rows.append((f"{row['peer']}: {row['name']}", "file", None, None))
                    if row["status"] == "offer":
                        sidebar_rows.append(("[Accept] [Reject]", "file", "file_buttons", row["id"]))
                    else:
                        sidebar_rows.append((f"{row['status']} {progress_bar(row, 6)}", "file", None, None))
                if not dock_rows:
                    sidebar_rows.append(("No active transfers", "muted", None, None))
                if role == "admin":
                    sidebar_rows += [(f"PENDING ({len(pending)})", "admin", None, None)]
                    for name in pending:
                        sidebar_rows.append((name, "admin", "pending", name))
                        if sidebar - 3 >= 19:
                            sidebar_rows.append(("[Approve] [Reject]", "admin", "buttons", name))
                for y, (label, style, kind, name) in enumerate(
                    sidebar_rows[: max(0, dock_y - 3)], 3
                ):
                    if kind == "file_buttons":
                        write(y, x + 1, "[Accept]", 8, "success")
                        write(y, x + 10, "[Reject]", 8, "error")
                        hits.append((y, x + 1, x + 9, ("accept_file", name)))
                        hits.append((y, x + 10, x + 18, ("command", f"/reject {name}")))
                        continue
                    if kind == "buttons" and valid_username(name):
                        write(y, x + 1, "[Approve]", sidebar - 3, "success")
                        reject_x = x + 1 + 9 + 1
                        write(y, reject_x, "[Reject]", width - reject_x - 2, "error")
                        hits.append((y, x + 1, x + 10, ("command", f"/approve {name}")))
                        hits.append(
                            (y, reject_x, reject_x + 8, ("command", f"/reject-user {name}"))
                        )
                        continue
                    text = safe_display(label)
                    visible_label = fit_cells(text, sidebar - 3)
                    write(y, x + 1, visible_label, sidebar - 3, style)
                    if name is not None and valid_username(name):
                        hits.append(
                            (y, x + 1, x + 1 + cell_width(visible_label), ("person", kind, name))
                        )
            # The dock always keeps rows for offers: Accept/Reject stay
            # clickable even when the sidebar has more people than fits.
            for y, row in enumerate(dock_rows[:dock_height], dock_y):
                if row["status"] == "offer" and not sidebar:
                    buttons = "[Accept] [Reject]"
                    room = max(1, width - 4 - cell_width(buttons) - 1)
                    label = fit_cells(
                        f"INCOMING {safe_display(row['peer'])}: {safe_display(row['name'])}", room
                    )
                    accept_x = 2 + cell_width(label) + 1
                    reject_x = accept_x + cell_width("[Accept]") + 1
                    write(y, 2, label, width - 4, "muted")
                    write(y, accept_x, "[Accept]", width - accept_x - 2, "success")
                    write(y, reject_x, "[Reject]", width - reject_x - 2, "error")
                    hits.append((y, accept_x, accept_x + 8, ("accept_file", row["id"])))
                    hits.append((y, reject_x, reject_x + 8, ("command", f"/reject {row['id']}")))
                else:
                    label = f"{row['direction']} {row['id'][:8]} {fit_cells(row['name'], 12)} {progress_bar(row, 10)}"
                    write(y, 2, safe_display(label), width - 4, "file")
            if popup_height:
                start = max(0, selection - popup_height + 1)
                for row, index in enumerate(range(start, min(len(options), start + popup_height))):
                    option = options[index]
                    label = f"{'›' if index == selection else ' '} {option.value} {option.usage}"
                    if width >= 75:
                        label = f"{label:<30} {option.description}"
                    write(
                        popup_y + row,
                        2,
                        label,
                        width - 4,
                        "selected" if index == selection else "muted",
                    )
                    hits.append(
                        (
                            popup_y + row,
                            2,
                            min(width - 2, 2 + cell_width(label)),
                            ("suggestion", index),
                        )
                    )
            write(input_y - 1, 2, "─" * (width - 4), width - 4)
            write(input_y, 2, "› ", 2, "accent")
            # Keep the edit cursor visible, including wide CJK characters.
            offset = 0
            while cell_width(composer.text[offset : composer.cursor]) > width - 7:
                offset += 1
            visible = composer.text[offset:]
            placeholder = (
                f"Message {direct_peer} privately, or / for commands"
                if direct_peer is not None
                else "Open a conversation, or / for commands"
                if session.display_filter == "dm"
                else "Message everyone, or / for commands"
            )
            write(
                input_y,
                4,
                visible or placeholder,
                width - 6,
                "accent" if visible else "muted",
            )
            guidance = composer_guidance(composer.text)
            ghost = (
                "path/to/file"
                if guidance and guidance.startswith("File:")
                else "message text"
                if guidance
                else ""
            )
            if ghost and composer.cursor == len(composer.text):
                write(
                    input_y,
                    4 + cell_width(visible),
                    ghost,
                    max(0, width - 5 - cell_width(visible)),
                    "muted",
                )
            entries = []
            if menu is not None:
                kind, target = menu[0], menu[1]
                if kind == "people":
                    entries = [
                        (f"● {name}", ("open_user", name))
                        for name in users
                        if valid_username(name)
                    ]
                    if role == "admin":
                        entries += [
                            (f"pending {name}", ("open_pending", name))
                            for name in pending
                            if valid_username(name)
                        ]
                elif kind == "user" and target in users and valid_username(target):
                    entries = [
                        ("Direct message", ("direct_peer", target)),
                        ("Send file", ("send_file", target)),
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
                    "People"
                    if menu[0] == "people"
                    else f"Pending: {menu[1]}"
                    if menu[0] == "pending"
                    else menu[1]
                )
                menu_visible = min(len(entries), max(1, input_y - 6))
                menu_start = min(
                    max(0, menu[2] - menu_visible + 1),
                    max(0, len(entries) - menu_visible),
                )
                if len(entries) > menu_visible:
                    title = f"{title} {menu[2] + 1}/{len(entries)}"
                menu_width = min(
                    width - 4,
                    max(
                        [cell_width(title)]
                        + [cell_width("  " + label) for label, _ in entries]
                    )
                    + 4,
                )
                menu_height = menu_visible + 1
                anchor_x, anchor_y = menu[3], menu[4]
                if anchor_x is None:
                    menu_x = max(2, (width - menu_width) // 2)
                    menu_y = max(4, (height - menu_height) // 2)
                else:
                    menu_x = min(max(2, anchor_x), width - menu_width - 2)
                    menu_y = min(max(4, anchor_y), input_y - 1 - menu_height)
                if menu_width >= 12 and menu_y >= 4:
                    write(menu_y, menu_x, " " * menu_width, menu_width, "muted")
                    write(menu_y, menu_x + 2, title, menu_width - 4, "accent")
                    for menu_offset, (label, _) in enumerate(
                        entries[menu_start : menu_start + menu_visible]
                    ):
                        row = menu_y + 1 + menu_offset
                        index = menu_start + menu_offset
                        write(
                            row,
                            menu_x + 1,
                            " " * (menu_width - 2),
                            menu_width - 2,
                            "muted",
                        )
                        write(
                            row,
                            menu_x + 2,
                            ("› " if index == menu[2] else "  ") + label,
                            menu_width - 4,
                            "selected" if index == menu[2] else "muted",
                        )
                        hits.append(
                            (row, menu_x, menu_x + menu_width, ("menu_item", index))
                        )
                else:
                    menu = None
            hint_style = "muted"
            if direct_flash is not None and monotonic() < direct_flash[2]:
                hint, hint_style = direct_flash[0], direct_flash[1]
            else:
                hint = (
                    "↑↓ choose · Enter select · Esc close"
                    if menu is not None
                    else "↑↓ choose · Tab complete · Enter use · Esc dismiss"
                    if options
                    else "Enter send private · Esc inbox · ↑↓/PgUp scroll · / cmds · F1 Help"
                    if direct_peer is not None
                    else "↑↓ choose · Enter open · Esc dismiss · / cmds · F1 Help"
                    if session.display_filter == "dm"
                    else guidance
                    if guidance
                    else "Enter send · / cmds · ↑↓ history · wheel scroll · right-click user · F7 people · F1 Help · Ctrl+C exit"
                    if sidebar
                    else "Enter send · / cmds · ↑↓ history · PgUp/wheel scroll · F7 people · F1 Help · Ctrl+C exit"
                )
            write(height - 1, 2, hint, width - 4, hint_style)
            try:
                window.move(input_y, 4 + cell_width(composer.text[offset : composer.cursor]))
                window.refresh()
                key = window.get_wch()
            except curses.error:
                continue
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
                            if (
                                hit
                                and hit[0] == "menu_item"
                                and 0 <= hit[1] < len(entries)
                            ):
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
                    if isinstance(alternate, str) and alternate in "1234":
                        session.display_filter = FILTERS[int(alternate) - 1]
                        scroll = 0
                    elif isinstance(alternate, str) and alternate.isprintable():
                        composer.insert(alternate)
                except curses.error:
                    pass
                finally:
                    window.timeout(100)
                if alternate is None and direct_peer is not None:
                    back_to_inbox()
            elif key == getattr(curses, "KEY_F1", -1):
                show_help = True
                help_scroll = 0
            elif key == getattr(curses, "KEY_F2", -2):
                sidebar_enabled = not sidebar_enabled
            elif key == getattr(curses, "KEY_F3", -3):
                session.display_filter = FILTERS[
                    (FILTERS.index(session.display_filter) + 1) % len(FILTERS)
                ]
                scroll = 0
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
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if options and composer.text != options[selection].value:
                    composer.replace(options[selection].value + " ")
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
                        if line.strip().lower() == "/help":
                            show_help = True
                            help_scroll = 0
                        elif line.strip().lower() == "/history":
                            try:
                                history_records = session.history_sessions()
                                history_selected = history_scroll = 0
                                history_lines = None
                            except (ValueError, OSError, sqlite3.Error) as error:
                                add(str(error), "error")
                        else:
                            if not line.startswith(("/", "@")):
                                line = (
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
                    direct_scroll += capacity
                else:
                    scroll += capacity
            elif key == curses.KEY_NPAGE:
                if direct_peer is not None:
                    direct_scroll = max(0, direct_scroll - capacity)
                else:
                    scroll = max(0, scroll - capacity)
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
                        else:
                            scroll += 3  # Wheel up: older messages.
                    elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                        if direct_peer is not None:
                            direct_scroll = max(0, direct_scroll - 3)
                        else:
                            scroll = max(0, scroll - 3)  # Wheel down: back to live.
                    elif right:
                        hit = hit_at(mouse_x, mouse_y)
                        if hit and hit[0] == "person":
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
                            if (
                                hit
                                and hit[0] == "menu_item"
                                and 0 <= hit[1] < len(entries)
                            ):
                                if not perform(entries[hit[1]][1]):
                                    return
                            else:
                                menu = None
                        elif hit:
                            if hit[0] == "direct_peer":
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
                                session.display_filter = hit[1]
                                scroll = 0
                            elif hit[0] in {"command", "accept_file"}:
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
