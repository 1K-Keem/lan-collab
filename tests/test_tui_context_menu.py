"""Headless event-loop proof for TUI menus, click hitboxes and prefills.

The runtime Python here cannot import the native _curses extension, so a
fake curses module drives the real run_tui loop: frames are rendered by the
production layout code and clicks are aimed at the text those frames emitted.
No native visual proof is implied; hitbox correctness is proven by the
rendered coordinates the click helpers resolve.
"""

from collections import deque
from threading import RLock
from types import ModuleType
import sys
import unittest
from unittest.mock import patch

from lancollab.client.application import ClientState
from lancollab.client.tui import run_tui

OFFER_ID = "12345678-aaaa-bbbb-cccc-1234567890ab"

class FakeFiles:
    def __init__(self, rows=()):
        self.rows = [dict(row) for row in rows]
        self.accepted = []

    def snapshot(self):
        return [dict(row) for row in self.rows]

    def accept(self, transfer_id, *, directory=None):
        self.accepted.append((transfer_id, directory))

class FakeClient:
    def __init__(self, *, role="member", users=(), pending=(), transfers=()):
        self.state = ClientState.AUTHENTICATED
        self._lock = RLock()
        self.username = "alice"
        self.role = role
        self.online_users = {name: ("127.0.0.1", 1) for name in users}
        self.pending_users = [{"username": name} for name in pending]
        self.reply_target = None
        self.last_tcp_rtt = None
        self.files = FakeFiles(transfers)

class FakeSession:
    def __init__(self, **client):
        self.output = lambda _text: None
        self.client = FakeClient(**client)
        self.clear_generation = 0
        self.display_filter = "all"
        self.udp = None
        self.executed = []
        self.closed = False

    def execute(self, command):
        self.executed.append(command)
        return True

    def refresh_pending_quietly(self):
        pass

    def close(self):
        self.closed = True

class FakeWindow:
    """Minimal curses window that renders into rows and returns scripted input."""

    def __init__(self, rows, columns, events):
        self.rows = rows
        self.columns = columns
        self.events = deque(events)
        self.lines = {}
        self.mouse = deque()
        self.module = None

    def addstr(self, y, x, text, style=0):
        if not 0 <= y < self.rows or x < 0:
            raise self.module.error("outside window")
        if x >= self.columns:
            raise self.module.error("outside window")
        text = text[: self.columns - x]
        line = self.lines.get(y, "")
        if len(line) < x:
            line += " " * (x - len(line))
        self.lines[y] = line[:x] + text + line[x + len(text) :]

    def erase(self):
        self.lines = {}

    def getmaxyx(self):
        return (self.rows, self.columns)

    def keypad(self, _flag):
        pass

    def timeout(self, _value):
        pass

    def move(self, _y, _x):
        pass

    def refresh(self):
        pass

    def clearok(self, _flag):
        pass

    def get_wch(self):
        if not self.events:
            raise AssertionError("TUI asked for input after the script ended")
        item = self.events.popleft()
        if callable(item):
            item = item(self)
        if isinstance(item, str) and len(item) > 1:
            self.events.appendleft(item[1:])
            return item[0]
        return item

    def all_text(self):
        return "\n".join(self.lines[y] for y in sorted(self.lines))

    def find(self, text, occurrence=0):
        found = []
        for y in sorted(self.lines):
            line = self.lines[y]
            start = line.find(text)
            while start != -1:
                found.append((y, start))
                start = line.find(text, start + 1)
        return found[occurrence]

    def input_text(self):
        return self.lines.get(self.rows - 3, "")

    def click_mouse(self, x, y, bstate):
        self.mouse.append((0, x, y, 0, bstate))
        return self.module.KEY_MOUSE

def fake_curses(window):
    module = ModuleType("curses")
    module.window = window
    module.A_BOLD, module.A_DIM, module.A_REVERSE = 1, 2, 4
    module.COLOR_BLACK = module.COLOR_RED = module.COLOR_GREEN = 0
    module.COLOR_YELLOW = module.COLOR_BLUE = module.COLOR_MAGENTA = 0
    module.COLOR_CYAN = module.COLOR_WHITE = 0
    module.KEY_UP, module.KEY_DOWN = 258, 259
    module.KEY_LEFT, module.KEY_RIGHT = 260, 261
    module.KEY_HOME, module.KEY_END = 262, 360
    module.KEY_BACKSPACE, module.KEY_DC = 263, 330
    module.KEY_NPAGE, module.KEY_PPAGE = 338, 339
    module.KEY_ENTER, module.KEY_MOUSE, module.KEY_RESIZE = 343, 409, 410
    module.KEY_F1, module.KEY_F2, module.KEY_F3, module.KEY_F4 = 265, 266, 267, 268
    module.KEY_F5, module.KEY_F6, module.KEY_F7 = 269, 270, 271
    module.BUTTON1_CLICKED, module.BUTTON3_CLICKED = 1, 2
    module.BUTTON3_PRESSED, module.BUTTON4_PRESSED = 4, 8
    module.BUTTON5_PRESSED = 16

    class error(Exception):
        pass

    module.error = error
    module.has_colors = lambda: False
    module.start_color = lambda: None
    module.use_default_colors = lambda: None
    module.init_pair = lambda *_args: None
    module.color_pair = lambda _number: 0
    module.curs_set = lambda _value: None
    module.mouse_mask = 0
    module.program_mode = True
    module.def_prog_mode = lambda: None
    def endwin():
        module.program_mode = False
        module.mouse_mask = 0
    def reset_prog_mode():
        module.program_mode = True
    def mousemask(mask):
        module.mouse_mask = mask
        return 0
    module.endwin = endwin
    module.reset_prog_mode = reset_prog_mode
    module.mousemask = mousemask

    def getmouse():
        if not module.program_mode or not module.mouse_mask:
            raise error("terminal mouse reporting is disabled")
        if not window.mouse:
            raise error("no mouse event")
        return window.mouse.popleft()

    module.getmouse = getmouse

    def wrapper(function):
        return function(window)

    module.wrapper = wrapper
    return module

def click(label, *, right=False, occurrence=0):
    def event(window):
        y, x = window.find(label, occurrence)
        state = window.module.BUTTON3_CLICKED if right else window.module.BUTTON1_CLICKED
        return window.click_mouse(x, y, state)

    return event

def expect(condition, then):
    """Assert the frame just drawn, then supply the next event."""

    def event(window):
        assert condition(window), window.all_text()
        return then(window) if callable(then) else then

    return event

def run(session, events, rows=24, columns=100):
    window = FakeWindow(rows, columns, events)
    module = fake_curses(window)
    window.module = module
    with patch.dict(sys.modules, {"curses": module}):
        run_tui(session)
    return window

class ContextMenuEventLoopTests(unittest.TestCase):
    def test_send_file_picker_preserves_spaces_and_cancel_sends_nothing(self):
        for path in (r"C:\My files\notes.txt", ""):
            with self.subTest(path=path):
                session = FakeSession(users=("alice", "bob"))
                events = [
                    expect(lambda w: "● alice" not in w.all_text(), click("bob", right=True)),
                    click("Send file"), "\x03",
                ]
                with patch("lancollab.client.tui.choose_file", return_value=path):
                    run(session, events)
                if path:
                    self.assertEqual(session.executed[-1].args, ("bob", path))
                    self.assertEqual(session.executed[-1].name, "/send")
                else:
                    self.assertEqual(session.executed, [])

    def test_picker_error_keeps_chat_usable(self):
        session = FakeSession(users=("bob",))
        with patch("lancollab.client.tui.choose_file", side_effect=RuntimeError("Picker unavailable")):
            run(session, [click("bob"), click("Send file"),
                          expect(lambda w: "Picker unavailable" in w.all_text(), "\x03")])
        self.assertEqual(session.executed, [])

    def test_left_click_is_the_menu_fallback(self):
        session = FakeSession(users=("bob",))
        events = [
            click("bob"),
            expect(
                lambda w: "Direct message" in w.all_text() and "Send file" in w.all_text(),
                "\x03",
            ),
        ]
        run(session, events)
        self.assertEqual(session.executed, [])

    def test_accept_and_reject_buttons_use_full_offer_id(self):
        offer = dict(
            id=OFFER_ID,
            direction="in",
            name="notes.txt",
            peer="bob",
            done=0,
            size=10,
            status="offer",
        )
        for folder in (r"C:\Saved files", ""):
            session = FakeSession(users=("bob",), transfers=(offer,))
            def sidebar_accept(window):
                y, x = window.find("[Accept]")
                self.assertGreaterEqual(x, window.columns - 24)
                return window.click_mouse(x, y, window.module.BUTTON1_CLICKED)
            with patch("lancollab.client.tui.choose_file", return_value=folder):
                run(session, [sidebar_accept, "\x03"])
            self.assertEqual(session.client.files.accepted, [(OFFER_ID, folder)] if folder else [])
        session = FakeSession(users=("bob",), transfers=(offer,))
        run(session, [click("[Reject]"), "\x03"])
        self.assertEqual(session.executed[-1].name, "/reject")
        self.assertEqual(session.executed[-1].args, (OFFER_ID,))

    def test_online_menu_remains_clickable_after_send_and_accept_dialogs(self):
        offer = dict(id=OFFER_ID, direction="in", name="notes.txt", peer="bob",
                     done=0, size=10, status="offer")
        for result in ("", r"C:\Saved files", RuntimeError("Picker failed")):
            session = FakeSession(users=("bob",), transfers=(offer,))
            window = FakeWindow(24, 100, [
                click("bob"), click("Send file"),
                click("bob", right=True),
                expect(lambda w: "Direct message" in w.all_text(), "\x1b"),
                click("[Accept]"), click("bob"),
                expect(lambda w: "Direct message" in w.all_text(), click("Direct message")),
                expect(lambda w: "/msg bob" in w.input_text(), "\x03"),
            ])
            module = fake_curses(window)
            window.module = module
            def picker(**kwargs):
                # A desktop popup takes focus away from the terminal.
                module.program_mode = False
                module.mouse_mask = 0
                if isinstance(result, Exception):
                    raise result
                return result
            with patch.dict(sys.modules, {"curses": module}), patch(
                "lancollab.client.tui.choose_file", side_effect=picker
            ):
                run_tui(session)

    def test_pending_sidebar_buttons_and_menu_use_backend_full_names(self):
        session = FakeSession(role="admin", pending=("carol", "dave"))
        events = [
            expect(lambda w: "[Approve]" in w.all_text(), click("[Approve]")),
            expect(lambda w: session.executed, click("dave")),
            expect(lambda w: "Pending: dave" in w.all_text(), click(" Reject")),
            expect(lambda w: session.executed, "\x03"),
        ]
        run(session, events)
        commands = [(command.name, command.args) for command in session.executed]
        self.assertIn(("/approve", ("carol",)), commands)
        self.assertIn(("/reject-user", ("dave",)), commands)

    def test_keyboard_f7_arrows_enter_open_menus_and_approve(self):
        session = FakeSession(role="admin", users=("bob",), pending=("carol",))
        module = None

        def f7_and_down(window):
            nonlocal module
            module = window.module
            window.events.appendleft(module.KEY_DOWN)
            return module.KEY_F7

        events = [
            f7_and_down,
            expect(lambda w: "People" in w.all_text(), module.KEY_ENTER if module else 343),
            expect(lambda w: "Pending: carol" in w.all_text(), 343),
            expect(lambda w: session.executed, "\x03"),
        ]
        run(session, events)
        self.assertEqual(session.executed[-1].name, "/approve")
        self.assertEqual(session.executed[-1].args, ("carol",))

    def test_long_people_menu_scrolls_to_selection(self):
        users = tuple(f"user{index:02d}" for index in range(20))
        session = FakeSession(users=users)
        events = [271] + [259] * 5  # F7 then five Down presses.
        events += [
            expect(lambda w: "People 6/20" in w.all_text(), 343),
            expect(lambda w: "Send file" in w.all_text(), "\x1b"),
            expect(lambda w: "Send file" not in w.all_text(), "\x03"),
        ]
        run(session, events, rows=12, columns=80)
        self.assertEqual(session.executed, [])

    def test_resize_closes_menu_and_hides_stale_sidebar_hitboxes(self):
        session = FakeSession(users=("bob",))

        def shrink(window):
            window.columns = 70
            return window.module.KEY_RESIZE

        events = [
            click("bob", right=True),
            expect(
                lambda w: "Direct message" in w.all_text(),
                shrink,
            ),
            expect(
                lambda w: "Direct message" not in w.all_text()
                and "ONLINE" not in w.all_text(),
                "\x03",
            ),
        ]
        run(session, events)
        self.assertEqual(session.executed, [])

if __name__ == "__main__":
    unittest.main()
