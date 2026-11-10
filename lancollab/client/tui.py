"""Optional standard-library curses interface using the same CLI commands."""

from collections import deque
from queue import Empty, Queue
import textwrap

from lancollab.client.application import ClientState, safe_display
from lancollab.client.cli import parse_command


def run_tui(session):
    import curses

    notices = Queue()
    original_output = session.output
    session.output = notices.put

    def screen(window):
        window.timeout(100)
        window.keypad(True)
        buffer = ""
        scroll = 0
        messages = deque(
            ["Type /help for commands. PageUp/PageDown scroll; Ctrl+C exits."], maxlen=2000
        )

        def write(y, x, text, width):
            try:
                window.addnstr(y, x, text, max(0, width))
            except curses.error:
                pass  # Terminal resized or a wide character hit the right edge.

        while session.client.state is not ClientState.CLOSED:
            while True:
                try:
                    messages.append(safe_display(notices.get_nowait()))
                except Empty:
                    break
            height, width = window.getmaxyx()
            window.erase()
            if height < 6 or width < 30:
                write(0, 0, "Enlarge the terminal (30 x 6 minimum)", width - 1)
            else:
                client = session.client
                write(
                    0,
                    0,
                    f"LAN Collab | {client.username} ({client.role}) | {client.state.value}",
                    width - 1,
                )
                write(1, 0, "Online: " + ", ".join(sorted(client.online_users)), width - 1)
                rows = [
                    line
                    for message in messages
                    for line in (textwrap.wrap(message, width - 2) or [""])
                ]
                capacity = height - 4
                scroll = min(scroll, max(0, len(rows) - capacity))
                end = len(rows) - scroll
                for y, line in enumerate(rows[max(0, end - capacity) : end], 2):
                    write(y, 0, line, width - 1)
                visible = buffer[-(width - 4) :]
                write(height - 1, 0, "> " + visible, width - 1)
                window.move(height - 1, min(width - 2, len(visible) + 2))
            window.refresh()
            try:
                key = window.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                line, buffer = buffer, ""
                if not line.strip():
                    continue
                try:
                    if not session.execute(parse_command(line)):
                        return
                except ValueError as error:
                    messages.append(str(error))
                scroll = 0
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                buffer = buffer[:-1]
            elif key == curses.KEY_PPAGE:
                scroll += max(1, height - 4)
            elif key == curses.KEY_NPAGE:
                scroll = max(0, scroll - max(1, height - 4))
            elif key == "\x04":
                return
            elif isinstance(key, str) and key.isprintable() and len(buffer) < 8192:
                buffer += key

    try:
        curses.wrapper(screen)
    except KeyboardInterrupt:
        pass
    finally:
        session.output = original_output
        session.close()
