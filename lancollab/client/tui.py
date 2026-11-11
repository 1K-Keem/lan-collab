"""Colored terminal workspace with slash completion; standard-library curses only."""

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
from lancollab.client.cli import _DETAILED_HELP, parse_command, progress_bar
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
        try:
            curses.mousemask(
                curses.BUTTON4_PRESSED | curses.BUTTON5_PRESSED | curses.BUTTON1_CLICKED
            )
        except curses.error:
            pass  # Mouse-less terminals keep keyboard scrolling.
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        composer = Composer()
        scroll, selection, dismissed = 0, 0, False
        show_help, help_scroll = False, 0
        history_records = None
        history_selected = history_scroll = 0
        history_lines = None
        history_wrapped_width = None
        history_rows = []
        sidebar_enabled = True
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
                    if scroll:
                        unseen += 1
                except Empty:
                    break
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
                users = sorted(client.online_users)
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
            dock_height = min(3, len(transfers), max(0, height - popup_height - 14))
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
            tab_hits = []
            tab_x = 2
            for category, label in tabs:
                write(
                    3,
                    tab_x,
                    label,
                    len(label),
                    "selected" if session.display_filter == category else "muted",
                )
                tab_hits.append((tab_x, tab_x + len(label), category))
                tab_x += len(label) + 2
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
            for y, (pre, line, style) in enumerate(rows[max(0, end - capacity) : end], 4):
                if pre:
                    write(y, 2, pre, chat_width, "muted")
                write(y, 2 + cell_width(pre), line, chat_width - cell_width(pre), style)
            user_hits = {}
            if sidebar:
                x = width - sidebar
                for y in range(3, dock_y):
                    write(y, x - 1, "│", 1)
                sidebar_rows = [("ONLINE", "accent", None)]
                sidebar_rows += [
                    ("● " + name, "success", name) for name in users[: max(1, (dock_y - 8) // 2)]
                ]
                sidebar_rows += [("TRANSFERS", "accent", None)]
                sidebar_rows += [
                    (f"{row['direction']} {row['id'][:8]} {row['status']}", "file", None)
                    for row in transfers[:2]
                ] or [("No active transfers", "muted", None)]
                if role == "admin":
                    sidebar_rows += [(f"PENDING ({len(pending)})", "admin", None)]
                    sidebar_rows += [(name, "admin", None) for name in pending]
                for y, (label, style, name) in enumerate(sidebar_rows[: max(0, dock_y - 3)], 3):
                    write(y, x + 1, safe_display(label), sidebar - 3, style)
                    if name is not None:
                        user_hits[y] = name
            for y, row in enumerate(transfers[:dock_height], dock_y):
                label = (
                    f"INCOMING /accept {row['id'][:8]} · {row['peer']}: {fit_cells(row['name'], 16)}"
                    if row["status"] == "offer"
                    else f"{row['direction']} {row['id'][:8]} {fit_cells(row['name'], 12)} {progress_bar(row, 10)}"
                )
                write(
                    y,
                    2,
                    safe_display(label),
                    width - 4,
                    "selected" if row["status"] == "offer" else "file",
                )
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
            write(input_y - 1, 2, "─" * (width - 4), width - 4)
            write(input_y, 2, "› ", 2, "accent")
            # Keep the edit cursor visible, including wide CJK characters.
            offset = 0
            while cell_width(composer.text[offset : composer.cursor]) > width - 7:
                offset += 1
            visible = composer.text[offset:]
            write(
                input_y,
                4,
                visible or "Message everyone, or / for commands",
                width - 6,
                "accent" if visible else "muted",
            )
            hint = (
                "↑↓ choose · Tab complete · Enter use · Esc dismiss"
                if options
                else "Enter send · / cmds · ↑↓ history · wheel scroll · click user · F1 Help · Ctrl+C exit"
                if sidebar
                else "Enter send · / cmds · ↑↓ history · PgUp/wheel scroll · F1 Help · Ctrl+C exit"
            )
            write(height - 1, 2, hint, width - 4)
            try:
                window.move(input_y, 4 + cell_width(composer.text[offset : composer.cursor]))
                window.refresh()
                key = window.get_wch()
            except curses.error:
                continue
            before = composer.text
            if key in ("\x03", "\x04"):
                return
            if key == "\x1b":
                dismissed = True
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
                session.execute(parse_command("/accept"))
            elif key == "\x0b":
                composer.replace(composer.text[: composer.cursor])
            elif key == "\x0c":
                session.execute(parse_command("/clear"))
            elif key == "\t" and options:
                composer.replace(options[selection].value + " ")
            elif key in ("\n", "\r", curses.KEY_ENTER):
                if options and composer.text != options[selection].value:
                    composer.replace(options[selection].value + " ")
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
                                line = "/all " + line
                            try:
                                if not session.execute(parse_command(line)):
                                    return
                            except ValueError as error:
                                add(str(error), "error")
                    scroll = 0
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                direction = -1 if key == curses.KEY_UP else 1
                if options:
                    selection = (selection + direction) % len(options)
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
                scroll += capacity
            elif key == curses.KEY_NPAGE:
                scroll = max(0, scroll - capacity)
            elif key == curses.KEY_MOUSE:
                try:
                    _, mouse_x, mouse_y, _, bstate = curses.getmouse()
                except curses.error:
                    pass
                else:
                    if bstate & curses.BUTTON4_PRESSED:
                        scroll += 3  # Wheel up: older messages.
                    elif bstate & curses.BUTTON5_PRESSED:
                        scroll = max(0, scroll - 3)  # Wheel down: back to live.
                    elif bstate & curses.BUTTON1_CLICKED:
                        if mouse_y == 3 and mouse_x < width - sidebar:
                            for left, right, category in tab_hits:
                                if left <= mouse_x < right:
                                    session.display_filter = category
                                    scroll = 0
                        elif sidebar and mouse_x >= width - sidebar:
                            if mouse_y in user_hits:
                                composer.replace(f"/msg {user_hits[mouse_y]} ")
                        elif popup_height and popup_y <= mouse_y < popup_y + popup_height:
                            start = max(0, selection - popup_height + 1)
                            index = start + (mouse_y - popup_y)
                            if index < len(options):
                                composer.replace(options[index].value + " ")
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
