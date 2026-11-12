"""Keyboard/mouse credentials form; credentials are collected before TLS opens."""

from lancollab.client.application import _USERNAME, safe_display
from lancollab.client.branding import banner_lines
from lancollab.client.tui import Composer, _palette, cell_width, fit_cells, wrap_cells


def prompt_auth(server, message="", username="", choice="login"):
    import curses

    def screen(window):
        styles = _palette(curses)
        window.keypad(True)
        try:
            curses.mousemask(getattr(curses, "BUTTON1_CLICKED", 0))
            curses.curs_set(1)
        except curses.error:
            pass
        fields = {name: Composer() for name in ("username", "password", "confirm")}
        fields["username"].replace(username)
        mode, focus, remember, error = choice, "username", False, message

        def switch(value):
            nonlocal mode, focus, error
            mode, focus, error = value, "username", ""
            fields["password"].replace("")
            fields["confirm"].replace("")

        while True:
            height, width = window.getmaxyx()
            window.erase()
            hits = []

            def write(y, x, text, room, style="muted"):
                try:
                    window.addstr(y, x, fit_cells(text, max(0, room)), styles.get(style, 0))
                except curses.error:
                    pass

            if height < 22 or width < 48:
                write(0, 0, "Resize to 48 x 22; Esc exits", width - 1)
                window.refresh()
                key = window.get_wch()
                if key in ("\x1b", "\x03", "\x04"):
                    return None
                continue
            left, top = max(2, (width - 56) // 2), max(0, (height - 22) // 2)
            room = min(56, width - left - 2)
            for row, line in enumerate(banner_lines(room)[:4]):
                write(top + row, left, line, room, "accent")
            write(top + 4, left, safe_display("Server: " + server), room)
            for x, value, label in (
                (left, "login", "[ Log in ]"),
                (left + 15, "signup", "[ Sign up ]"),
            ):
                write(top + 5, x, label, len(label), "selected" if mode == value else "accent")
                hits.append((top + 5, x, x + len(label), "mode:" + value))
            controls = (
                ["username", "password"]
                + (["confirm"] if mode == "signup" else ["remember"])
                + ["submit"]
            )
            positions = {"username": top + 7, "password": top + 10, "confirm": top + 13}
            cursor = None
            for name in ("username", "password", "confirm"):
                if name == "confirm" and mode != "signup":
                    continue
                y, editor = positions[name], fields[name]
                write(
                    y,
                    left,
                    {"username": "Username", "password": "Password", "confirm": "Confirm password"}[
                        name
                    ],
                    room,
                    "accent",
                )
                text = editor.text if name == "username" else "*" * len(editor.text)
                offset = max(0, editor.cursor - (room - 5))
                visible = fit_cells(text[offset:], room - 4)
                write(
                    y + 1,
                    left,
                    "[ " + visible.ljust(room - 4) + " ]",
                    room,
                    "selected" if focus == name else "muted",
                )
                hits.append((y + 1, left, left + room, name))
                if focus == name:
                    cursor = (y + 1, left + 2 + cell_width(text[offset : editor.cursor]))
            if mode == "login":
                label = ("[x]" if remember else "[ ]") + " Remember me on this device"
                write(top + 13, left, label, room, "selected" if focus == "remember" else "muted")
                hits.append((top + 13, left, left + len(label), "remember"))
            label = "[ Create account ]" if mode == "signup" else "[ Log in ]"
            write(top + 16, left, label, room, "selected" if focus == "submit" else "accent")
            hits.append((top + 16, left, left + len(label), "submit"))
            for index, line in enumerate(wrap_cells(safe_display(error), room)[:2]):
                write(top + 18 + index, left, line, room, "error")
            write(top + 21, left, "Tab: next | F2: Login/Signup | Enter: go | Esc: exit", room)
            try:
                curses.curs_set(1 if cursor else 0)
                if cursor:
                    window.move(*cursor)
                window.refresh()
                key = window.get_wch()
            except curses.error:
                continue
            if key in ("\x1b", "\x03", "\x04"):
                return None
            if key == getattr(curses, "KEY_RESIZE", -999):
                continue
            if key == curses.KEY_MOUSE:
                try:
                    _, x, y, _, state = curses.getmouse()
                except curses.error:
                    continue
                if not state & getattr(curses, "BUTTON1_CLICKED", 0):
                    continue
                target = next(
                    (target for row, a, b, target in hits if row == y and a <= x < b), None
                )
                if target is None:
                    continue
                if target.startswith("mode:"):
                    switch(target.split(":", 1)[1])
                    continue
                focus = target
                if target not in ("remember", "submit"):
                    continue
                key = "\n"
            if key in ("\t", curses.KEY_DOWN, curses.KEY_UP, getattr(curses, "KEY_BTAB", -998)):
                step = -1 if key in (curses.KEY_UP, getattr(curses, "KEY_BTAB", -998)) else 1
                focus = controls[(controls.index(focus) + step) % len(controls)]
                continue
            if key == getattr(curses, "KEY_F2", -997):
                switch("signup" if mode == "login" else "login")
                continue
            if key in ("\n", "\r", curses.KEY_ENTER) or (key == " " and focus == "remember"):
                if focus == "remember":
                    remember = not remember
                    continue
                if focus != "submit":
                    focus = controls[min(controls.index(focus) + 1, len(controls) - 1)]
                    continue
                user, password = fields["username"].text.strip(), fields["password"].text
                if _USERNAME.fullmatch(user) is None:
                    error, focus = "Username: 1-32 letters, digits, _, . or -.", "username"
                elif not password or (mode == "signup" and len(password) < 8):
                    error, focus = (
                        "Enter a password" if mode == "login" else "Use at least 8 characters.",
                        "password",
                    )
                elif mode == "signup" and password != fields["confirm"].text:
                    error, focus = "Passwords do not match; correct either field.", "confirm"
                else:
                    return mode, user, password, remember
                continue
            if focus in fields:
                editor = fields[focus]
                if key in (curses.KEY_BACKSPACE, "\b", "\x7f") and editor.cursor:
                    editor.text = editor.text[: editor.cursor - 1] + editor.text[editor.cursor :]
                    editor.cursor -= 1
                elif key == curses.KEY_DC:
                    editor.text = editor.text[: editor.cursor] + editor.text[editor.cursor + 1 :]
                elif key == curses.KEY_LEFT:
                    editor.cursor = max(0, editor.cursor - 1)
                elif key == curses.KEY_RIGHT:
                    editor.cursor = min(len(editor.text), editor.cursor + 1)
                elif key == curses.KEY_HOME:
                    editor.cursor = 0
                elif key == curses.KEY_END:
                    editor.cursor = len(editor.text)
                elif key == "\x15":
                    editor.replace("")
                elif isinstance(key, str) and key.isprintable():
                    editor.insert(key)

    return curses.wrapper(screen)
