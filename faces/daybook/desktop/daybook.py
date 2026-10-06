"""The page: the day the user is living, written as they go (LAYER.md).

One line in, one entry out. Plain words are a note. A line that starts with a verb does
something, and what it opens is a *thread*: one app filling the screen on its own sway
workspace, never beside another. The page remembers which entry opened which thread, so the
day's writing doubles as the way back to everything still open.

Each day is a file, `~/Daybook/YYYY-MM-DD.md`, one entry per line:

    09:12:05  → read rust borrow checker
    09:40:11  call the landlord about the heater
    10:02:40  → read https://example.org ‹from Claude›

Anything else in the file (the user's own edits) is shown as it is.
"""
from __future__ import annotations

import curses
import datetime as dt
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import textwrap
import threading
import time
import urllib.parse
from pathlib import Path

HOME = Path.home()
DAYS = HOME / "Daybook"
NOTES = HOME / "Notes"
RUN = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "daybook"
FOOT = ["foot", "-c", "/etc/face/foot.ini"]
# nvim in the terminal's palette (paper), as the editor window is (editor/init.lua).
NVIM = ["nvim", "--cmd", "set notermguicolors background=light title titlestring=%t"]
PAGE = "page"          # the page's workspace
CODE = "code"          # the editor window's workspace
EDITOR_APP = "raigolmi-editor"
HINT_FRESH = 20.0      # seconds a hint from page/init.lua stays good for

VERBS = {
    "read": "a page, or a search",
    "code": "the editor, or a file in it",
    "note": "a note of its own in ~/Notes; your words start it",
    "files": "your files",
    "shell": "a shell in the toolbelt",
    "ask": "talk to Claude",
    "find": "search every day",
}
ENTRY = re.compile(r"^(\d\d:\d\d:\d\d)  (→ )?(.*?)(?:  ‹(.*)›)?$")


# --- sway ------------------------------------------------------------------------------
class Sway:
    """Just enough of sway's IPC: commands, the tree, and a subscription on its own socket."""
    MAGIC = b"i3-ipc"

    def __init__(self) -> None:
        self.path = os.environ.get("SWAYSOCK") or str(
            Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"sway-ipc.{os.getuid()}.1.sock")
        self.lock = threading.Lock()
        self.sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.path)
        return s

    @classmethod
    def _send(cls, s, kind: int, payload: str = "") -> None:
        data = payload.encode()
        s.sendall(cls.MAGIC + struct.pack("=II", len(data), kind) + data)

    @staticmethod
    def _read(s, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = s.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("sway closed the socket")
            buf += chunk
        return buf

    @classmethod
    def _recv(cls, s):
        head = cls._read(s, 14)
        n, kind = struct.unpack("=II", head[6:])
        return kind, json.loads(cls._read(s, n))

    def call(self, kind: int, payload: str = ""):
        with self.lock:
            for attempt in (0, 1):
                try:
                    if self.sock is None:
                        self.sock = self._connect()
                    self._send(self.sock, kind, payload)
                    return self._recv(self.sock)[1]
                except OSError:
                    self.sock = None
                    if attempt:
                        raise

    def cmd(self, command: str):
        try:
            return self.call(0, command)
        except OSError:
            return None

    def tree(self):
        return self.call(4)

    def subscribe(self, events: list[str], into: queue.Queue) -> None:
        def run():
            while True:
                try:
                    s = self._connect()
                    self._send(s, 2, json.dumps(events))
                    self._recv(s)
                    while True:
                        into.put(self._recv(s))
                except OSError:
                    time.sleep(1)
        threading.Thread(target=run, daemon=True).start()


def windows(node, workspace=None):
    """(window, its workspace's name) for every window under `node`."""
    if node.get("type") == "workspace":
        workspace = node.get("name")
    kids = node.get("nodes", []) + node.get("floating_nodes", [])
    if not kids and node.get("type") in ("con", "floating_con") and node.get("pid"):
        yield node, workspace
    for kid in kids:
        yield from windows(kid, workspace)


def focused_workspace(tree):
    def walk(node, ws=None):
        if node.get("type") == "workspace":
            ws = node
        if node.get("focused"):
            return ws
        for kid in node.get("nodes", []) + node.get("floating_nodes", []):
            found = walk(kid, ws)
            if found is not None:
                return found
        return None
    return walk(tree)


# --- the days ---------------------------------------------------------------------------
class Entry:
    def __init__(self, day: str, time_: str, action: bool, text: str, via: str | None):
        self.day, self.time, self.action, self.text, self.via = day, time_, action, text, via

    @property
    def key(self):
        return (self.day, self.time, self.text)

    @property
    def verb(self) -> str:
        return self.text.split(" ", 1)[0] if self.action else ""

    @property
    def arg(self) -> str:
        return self.text.split(" ", 1)[1].strip() if self.action and " " in self.text else ""


class Days:
    def __init__(self) -> None:
        DAYS.mkdir(parents=True, exist_ok=True)
        self.cache: dict[str, tuple[float, list[Entry]]] = {}

    @staticmethod
    def path(day: str) -> Path:
        return DAYS / f"{day}.md"

    def entries(self, day: str) -> list[Entry]:
        path = self.path(day)
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return []
        cached = self.cache.get(day)
        if cached and cached[0] == mtime:
            return cached[1]
        out = []
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip() or line.startswith("# "):
                continue
            m = ENTRY.match(line)
            if m:
                out.append(Entry(day, m[1], bool(m[2]), m[3], m[4]))
            else:
                out.append(Entry(day, "", False, line.strip(), None))
        self.cache[day] = (mtime, out)
        return out

    def changed(self, day: str) -> bool:
        try:
            mtime = self.path(day).stat().st_mtime
        except FileNotFoundError:
            return day in self.cache
        cached = self.cache.get(day)
        return cached is None or cached[0] != mtime

    def write(self, text: str, action: bool, via: str | None = None) -> Entry:
        now = dt.datetime.now()
        day, stamp = now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S")
        path = self.path(day)
        head = "" if path.exists() else f"# {now.strftime('%A %-d %B %Y')}\n\n"
        line = f"{stamp}  {'→ ' if action else ''}{text}{f'  ‹{via}›' if via else ''}"
        with path.open("a") as f:
            f.write(f"{head}{line}\n")
        return Entry(day, stamp, action, text, via)

    def all_days(self) -> list[str]:
        return sorted(p.stem for p in DAYS.glob("????-??-??.md"))


# --- the page ---------------------------------------------------------------------------
class Page:
    def __init__(self, scr) -> None:
        self.scr = scr
        self.sway = Sway()
        self.days = Days()
        self.events: queue.Queue = queue.Queue()
        self.sway.subscribe(["window", "workspace"], self.events)
        RUN.mkdir(parents=True, exist_ok=True)
        self.log = open(RUN / "apps.log", "a")
        self.line = ""
        self.cursor = 0
        self.selected: int | None = None   # index into self.rows' selectable rows
        self.viewing = self.today()
        self.threads: dict[int, dict] = {}  # con id → {"entry": key|None, "title", "app"}
        self.pending: list[dict] = []
        self.next_ws = 1
        self.editor: int | None = None
        self.hints_seen: dict[str, float] = {}
        self.flash = ""
        self.flash_at = 0.0
        self.dirty = True
        self.minute = ""
        self.scan()
        self.sway.cmd(f"workspace {PAGE}")

    @staticmethod
    def today() -> str:
        return dt.date.today().isoformat()

    # --- threads ---------------------------------------------------------------------
    def scan(self) -> None:
        """What is open already: a page that restarted finds its threads again, unlinked."""
        try:
            tree = self.sway.tree()
        except OSError:
            return
        for win, ws in windows(tree):
            app = win.get("app_id") or ""
            if app == "daybook":
                if ws != PAGE:
                    self.sway.cmd(f"[con_id={win['id']}] move container to workspace {PAGE}")
                continue
            if ws and ws.startswith("t") and ws[1:].isdigit():
                self.next_ws = max(self.next_ws, int(ws[1:]) + 1)
            if app == EDITOR_APP:
                self.adopt_editor(win, ws)
            if win["type"] != "floating_con":
                self.threads[win["id"]] = {"entry": None, "title": win.get("name") or app,
                                           "app": app}

    def adopt_editor(self, win, ws) -> None:
        self.editor = win["id"]
        if ws != CODE:
            self.sway.cmd(f"[con_id={win['id']}] move container to workspace {CODE}")

    def on_event(self, kind: int, ev) -> None:
        if kind == 0x80000003:          # window
            con = ev.get("container", {})
            cid, change = con.get("id"), ev.get("change")
            if change == "new":
                self.on_new(con)
            elif change == "close":
                self.threads.pop(cid, None)
                if cid == self.editor:
                    self.editor = None
                self.home_if_empty()
            elif change == "title" and cid in self.threads:
                self.threads[cid]["title"] = con.get("name") or self.threads[cid]["title"]
            self.dirty = True
        elif kind == 0x80000000:        # workspace
            self.dirty = True

    def on_new(self, con) -> None:
        app = con.get("app_id") or ""
        if app == "daybook" or con.get("type") == "floating_con":
            return
        cid = con["id"]
        try:
            placed = {w["id"]: ws for w, ws in windows(self.sway.tree())}
        except OSError:
            placed = {}
        if app == EDITOR_APP:
            self.adopt_editor(con, placed.get(cid))
            self.threads[cid] = {"entry": None, "title": con.get("name") or "editor", "app": app}
            return
        entry = self.claim(con)
        self.threads[cid] = {"entry": entry, "title": con.get("name") or app, "app": app}
        # One thing on screen, always: a window that lands on the page or beside another
        # gets a workspace of its own, and the user is taken to it.
        ws = placed.get(cid)
        alone = ws and ws != PAGE and sum(1 for w in placed.values() if w == ws) == 1
        if not alone:
            ws = f"t{self.next_ws}"
            self.next_ws += 1
            self.sway.cmd(f"[con_id={cid}] move container to workspace {ws}")
        self.sway.cmd(f"workspace {ws}")

    def claim(self, con):
        """The entry a new window belongs to: one the page just launched, one Claude showed,
        or a fresh one saying where it came from."""
        for p in self.pending:
            if p["pid"] == con.get("pid") or (p["app"] and p["app"] == con.get("app_id")):
                self.pending.remove(p)
                return p["entry"]
        shown = self.hint("shown")
        if shown:
            return self.days.write(f"read {shown}", True, "from Claude").key
        app = con.get("app_id") or "window"
        verb = {"firefox": "read", "foot": "shell"}.get(app, app)
        return self.days.write(verb, True, "opened on its own").key

    def hint(self, name: str) -> str | None:
        """A line left in RUN/<name> by page or init.lua, once, while fresh."""
        path = RUN / name
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            return None
        if self.hints_seen.get(name) == mtime or time.time() - mtime > HINT_FRESH:
            return None
        self.hints_seen[name] = mtime
        return path.read_text().strip() or None

    def home_if_empty(self) -> None:
        try:
            ws = focused_workspace(self.sway.tree())
        except OSError:
            return
        if ws is not None and ws.get("type") == "workspace" and not ws.get("nodes") \
                and not ws.get("floating_nodes"):
            self.sway.cmd(f"workspace {PAGE}")

    def open_thread(self, entry: Entry) -> bool:
        for cid, t in self.threads.items():
            if t["entry"] == entry.key:
                self.sway.cmd(f"[con_id={cid}] focus")
                return True
        return False

    def thread_of(self, entry: Entry):
        for cid, t in self.threads.items():
            if t["entry"] == entry.key:
                return cid, t
        return None

    # --- doing ---------------------------------------------------------------------------
    def spawn(self, cmd: list[str], entry: Entry | None, app: str | None = None,
              cwd: Path | None = None) -> None:
        """Run `cmd`. One that opens a thread is started on a fresh workspace, so its window
        is born the size of the screen; if none arrives, the user is brought back to the page."""
        self.log.write(f"{dt.datetime.now():%H:%M:%S} {cmd}\n")
        self.log.flush()
        ws = None
        if entry is not None:
            ws = f"t{self.next_ws}"
            self.next_ws += 1
            self.sway.cmd(f"workspace {ws}")
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd or HOME), stdin=subprocess.DEVNULL,
                                    stdout=self.log, stderr=self.log, start_new_session=True)
        except OSError as exc:
            self.flash = f"could not start {cmd[0]}: {exc}"
            self.sway.cmd(f"workspace {PAGE}")
            return
        if entry is not None:
            self.pending.append({"entry": entry.key, "pid": proc.pid, "app": app, "ws": ws,
                                 "what": entry.text, "until": time.monotonic() + 30})

    def expire(self) -> None:
        """A launch that opened no window: off its blank workspace, back to the page."""
        now = time.monotonic()
        for p in [p for p in self.pending if p["until"] <= now]:
            self.pending.remove(p)
            try:
                ws = focused_workspace(self.sway.tree())
            except OSError:
                continue
            if ws and ws.get("name") == p["ws"] and not ws.get("nodes"):
                self.sway.cmd(f"workspace {PAGE}")
                self.flash = f"“{p['what']}” opened nothing (log: {RUN / 'apps.log'})"
                self.dirty = True

    def editor_socket(self) -> str | None:
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if argv and argv[0].endswith(b"nvim") and b"--listen" in argv:
                i = argv.index(b"--listen")
                if i + 1 < len(argv):
                    return argv[i + 1].decode()
        return None

    @staticmethod
    def where(arg: str, base: Path) -> Path:
        p = Path(os.path.expanduser(arg))
        return p if p.is_absolute() else base / p

    def run(self, entry: Entry) -> None:
        """Do what an action entry says: go to its thread, or open it again."""
        if self.open_thread(entry):
            return
        verb, arg = entry.verb, entry.arg
        if verb == "read":
            self.spawn(["/etc/face/firefox", "--new-window", self.url(arg)], entry, app="firefox")
        elif verb == "code":
            work = Path("/work") if Path("/work").is_dir() else HOME
            sock = self.editor_socket()
            if self.editor is not None and sock:
                if arg:
                    subprocess.run(["nvim", "--server", sock, "--remote",
                                    str(self.where(arg, work))], timeout=5,
                                   stdout=self.log, stderr=self.log)
                self.threads.setdefault(self.editor, {"title": "editor", "app": EDITOR_APP})
                self.threads[self.editor]["entry"] = entry.key
                self.sway.cmd(f"[con_id={self.editor}] focus")
            else:
                self.spawn([*FOOT, "--app-id=thread-code", *NVIM,
                            *([str(self.where(arg, work))] if arg else [])], entry, cwd=work)
        elif verb == "note":
            NOTES.mkdir(parents=True, exist_ok=True)
            target = self.note_file(arg) if arg else NOTES
            if arg and not target.exists():
                target.write_text(arg + "\n")
            self.spawn([*FOOT, "--app-id=thread-note", *NVIM, "-c", "normal! G$", str(target)], entry,
                       cwd=NOTES)
        elif verb == "files":
            place = self.where(arg, HOME) if arg else HOME
            self.spawn([*FOOT, "--app-id=thread-files", "yazi", str(place)], entry,
                       cwd=place if place.is_dir() else HOME)
        elif verb == "shell":
            self.spawn([*FOOT, "--app-id=thread-shell", "--title=shell", "python3", "-m", "rai", "terminal",
                        *([arg] if arg else [])], entry)
        elif verb == "ask":
            if arg and shutil.which("wl-copy"):
                # Both selections: right-click pastes the primary one (/guide/faces.md).
                for which in ([], ["--primary"]):
                    subprocess.run(["wl-copy", *which, "--", arg], timeout=5,
                                   stdout=self.log, stderr=self.log)
                self.flash = "your words are on the clipboard: right-click in the terminal"
            self.spawn(["python3", "-m", "rai", "ai", "--show"], None)
        else:  # an entry from a window that opened on its own; nothing to open again
            self.flash = "that thread has closed"

    @staticmethod
    def note_file(words: str) -> Path:
        """The note `note <words>` means: one named so already, else the file its words start,
        named from their first few (the same words always find the same note)."""
        named = [NOTES / words, NOTES / f"{words}.md", NOTES / f"{words}.txt"]
        for path in named:
            if path.is_file():
                return path
        slug = "-".join(re.findall(r"[^\W_]+", words.lower())[:6])[:48] or "note"
        return NOTES / f"{slug}.md"

    @staticmethod
    def url(arg: str) -> str:
        if not arg:
            return "about:blank"
        if re.match(r"^[a-z][a-z0-9+.-]*://", arg, re.I) or arg.startswith("about:"):
            return arg
        if " " not in arg and re.match(r"^[\w.-]+\.[a-z]{2,}(:\d+)?(/.*)?$|^localhost(:\d+)?",
                                       arg, re.I):
            return ("http://" if arg.startswith("localhost") else "https://") + arg
        if re.match(r"^[\w-]+\.[\w-]+:\d+", arg):   # a sandbox's page: myapi.tab-2:8000
            return "http://" + arg
        return "https://duckduckgo.com/?q=" + urllib.parse.quote_plus(arg)

    @staticmethod
    def parse(line: str):
        """(verb or None, the rest). A leading - or " keeps a line a note whatever it says."""
        text = line.strip()
        if not text:
            return None, ""
        if text[0] in "-\"'":
            return None, text.lstrip("-\"' ").strip()
        head, _, rest = text.partition(" ")
        if head.lower() in VERBS:
            return head.lower(), rest.strip()
        return None, text

    def hint_for(self, line: str) -> str:
        verb, rest = self.parse(line)
        if not line.strip():
            return "write anything to keep it — or begin with a verb (tab completes)"
        if verb is None:
            return "⏎ keep it on the page"
        if verb == "read":
            if not rest:
                return "⏎ a blank page"
            url = self.url(rest)
            return f"⏎ search the web for “{rest}”" if "duckduckgo" in url else f"⏎ open {url}"
        if verb == "code":
            return f"⏎ edit {rest}" if rest else "⏎ go to the editor"
        if verb == "note":
            if not rest:
                return "⏎ open ~/Notes"
            target = self.note_file(rest)
            if target.exists():
                return f"⏎ open the note ~/Notes/{target.name}"
            return f"⏎ a new note, ~/Notes/{target.name}, that starts with these words"
        if verb == "files":
            return f"⏎ browse {rest}" if rest else "⏎ browse your home"
        if verb == "shell":
            return f"⏎ a shell in {rest}'s toolbelt" if rest else "⏎ a shell in the focused toolbelt"
        if verb == "ask":
            return ("⏎ talk to Claude, your words on the clipboard" if rest
                    else "⏎ talk to Claude")
        if verb == "find":
            return "matches from every day · ↑ to pick one · esc to stop"
        return ""

    def submit(self) -> None:
        verb, rest = self.parse(self.line)
        self.line, self.cursor, self.flash = "", 0, ""
        self.viewing = self.today()
        if verb is None:
            if rest:
                self.days.write(rest, False)
            return
        if verb == "find":
            return
        entry = self.days.write(f"{verb} {rest}".strip(), True)
        self.run(entry)

    def complete(self) -> None:
        verb, rest = self.parse(self.line)
        head = self.line.strip()
        if " " not in head:
            options = [v for v in VERBS if v.startswith(head.lower())]
            if len(options) == 1:
                self.line = options[0] + " "
                self.cursor = len(self.line)
            return
        if verb in ("code", "files", "note"):
            base = {"code": Path("/work"), "files": HOME, "note": NOTES}[verb]
            if verb == "note" and " " in rest:
                return
            path = self.where(rest, base) if rest else base
            parent, stem = (path, "") if rest.endswith("/") or not rest else (path.parent, path.name)
            try:
                found = sorted(p for p in parent.iterdir() if p.name.startswith(stem)
                               and (stem.startswith(".") or not p.name.startswith(".")))
            except OSError:
                return
            if not found:
                return
            common = os.path.commonprefix([p.name for p in found])
            if len(found) == 1 and found[0].is_dir():
                common += "/"
            done = rest[: len(rest) - len(stem)] + common if rest else common
            self.line = f"{verb} {done}"
            self.cursor = len(self.line)
            if len(found) > 1:
                self.flash = "  ".join(p.name + ("/" if p.is_dir() else "") for p in found[:12])

    # --- drawing -------------------------------------------------------------------------
    def rows(self):
        """What the page lists now: (entry or None, day label, still-open thread or None)."""
        verb, rest = self.parse(self.line)
        if verb == "find" and len(rest) >= 2:
            needle = rest.lower()
            out = []
            for day in self.days.all_days():
                for e in self.days.entries(day):
                    if needle in e.text.lower():
                        out.append((e, day, None))
            return out
        out = [(e, None, None) for e in self.days.entries(self.viewing)]
        if self.viewing == self.today():
            # Threads no entry on this page accounts for: opened before the page restarted,
            # or from an earlier day. Listed last, so they can be chosen like any entry.
            listed = {e.key for e, _, _ in out}
            for cid, t in self.threads.items():
                if t["entry"] not in listed:
                    out.append((None, None, (cid, t)))
        return out

    def draw(self) -> None:
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 10 or w < 40:
            scr.addstr(0, 0, "the page needs more room"[: w - 1])
            scr.refresh()
            return
        width = min(w - 8, 100)
        left = (w - width) // 2
        ink, dim, accent, mark, sel = (curses.color_pair(i) for i in range(5))
        bold = curses.A_BOLD

        # The head: which day, and the time.
        now = dt.datetime.now()
        day = dt.date.fromisoformat(self.viewing)
        title = day.strftime("%A %-d %B") if self.viewing == self.today() else \
            day.strftime("%A %-d %B %Y") + "   (pgdn: back to today)"
        verb, rest = self.parse(self.line)
        if verb == "find" and len(rest) >= 2:
            title = f"every day, where it says “{rest}”"
        self.put(2, left, title, bold)
        opened = len(self.threads)
        right = f"{opened} open  ·  {now:%H:%M}" if opened else f"{now:%H:%M}"
        self.put(2, left + width - len(right), right, dim)

        # The day.
        top, bottom = 4, h - 7
        rows = self.rows()
        choosable = [i for i, r in enumerate(rows) if (r[0] and r[0].action) or r[2]]
        lines: list[tuple[int, int, str]] = []   # (row index, wrapped line no, text)
        text_width = width - 10
        for i, (e, label, thread) in enumerate(rows):
            if e is None:
                lines.append((i, 0, ""))
                continue
            wrapped = textwrap.wrap(e.text, text_width) or [""]
            for n, part in enumerate(wrapped):
                lines.append((i, n, part))
        if not any(e for e, _, _ in rows) and self.viewing == self.today() \
                and not self.line.strip():
            self.intro(top, left, width)
            top = min(bottom, top + len(VERBS) + 8)
        sel_row = choosable[self.selected] if self.selected is not None and \
            self.selected < len(choosable) else None
        room = max(1, bottom - top + 1)
        # The newest at the bottom, scrolled back as far as the chosen row needs.
        start = max(0, len(lines) - room)
        if sel_row is not None:
            first = min(n for n, l in enumerate(lines) if l[0] == sel_row)
            start = min(start, max(0, first - room // 3))
        end = start + room
        if start > 0:
            self.put(top - 1, left, "↑ earlier", dim)
        y = top
        for i, n, part in lines[start:end]:
            e, label, thread = rows[i]
            chosen = i == sel_row
            base = sel if chosen else ink
            if chosen:
                self.put(y, left - 2, " " * (width + 4), sel)
            if e is None:
                cid, t = thread
                self.put(y, left, "open", (sel if chosen else dim))
                self.put(y, left + 10, "● ", (sel if chosen else mark))
                self.put(y, left + 12, self.clip(t["title"], width - 12), base)
            else:
                if n == 0:
                    stamp = dt.date.fromisoformat(label).strftime("%-d %b") if label \
                        else e.time[:5]
                    self.put(y, left, stamp,
                             (sel if chosen else dim))
                if e.action and n == 0:
                    v = e.verb
                    self.put(y, left + 10, v, (sel | bold if chosen else accent | bold))
                    after = part[len(v):]
                    self.put(y, left + 10 + len(v), after, base)
                    tail = left + 10 + len(part) + 2
                    found = self.thread_of(e)
                    via = f"‹{e.via}›  " if e.via else ""
                    info = f"{via}● {found[1]['title']}" if found else via.strip()
                    if info and tail < left + width - 4:
                        self.put(y, tail, self.clip(info, left + width - tail),
                                 (sel if chosen else (mark if found else dim)))
                else:
                    self.put(y, left + 10, part, base)
            y += 1

        # The line the user writes on.
        self.put(h - 5, left, "─" * width, dim)
        self.put(h - 4, left, "›", accent | bold)
        shown = self.line
        room_line = width - 4
        offset = max(0, self.cursor - room_line + 1)
        self.put(h - 4, left + 2, shown[offset:offset + room_line], ink)
        self.put(h - 3, left + 2, self.clip(self.flash or self.hint_for(self.line), width - 2), dim)
        keys = "super+space the page · super+tab back · super+w close · ↑↓ choose · pgup earlier days"
        self.put(h - 2, left + max(0, (width - len(keys)) // 2), self.clip(keys, width), dim)

        if self.selected is None:
            curses.curs_set(1)
            scr.move(h - 4, left + 2 + self.cursor - offset)
        else:
            curses.curs_set(0)
        scr.refresh()

    def intro(self, top: int, left: int, width: int) -> None:
        dim, accent = curses.color_pair(1), curses.color_pair(2)
        self.put(top, left + 10, "Nothing yet today.", curses.color_pair(0))
        self.put(top + 2, left + 10, "Write anything and it stays here. Begin with a verb to do something:", dim)
        for n, (v, what) in enumerate(VERBS.items()):
            self.put(top + 4 + n, left + 12, v, accent | curses.A_BOLD)
            self.put(top + 4 + n, left + 20, what, dim)
        self.put(top + 5 + len(VERBS), left + 10,
                 "What you open fills the screen. super+space always brings you back here.", dim)

    @staticmethod
    def clip(s: str, n: int) -> str:
        return s if len(s) <= n else s[: max(0, n - 1)] + "…"

    def put(self, y: int, x: int, s: str, attr=0) -> None:
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, max(0, x), s[: max(0, w - max(0, x) - 1)], attr)
            except curses.error:
                pass

    # --- keys ----------------------------------------------------------------------------
    def key(self, k) -> None:
        rows = self.rows()
        choosable = [i for i, r in enumerate(rows) if (r[0] and r[0].action) or r[2]]
        if k in (curses.KEY_UP,):
            if choosable:
                self.selected = len(choosable) - 1 if self.selected is None else max(0, self.selected - 1)
        elif k in (curses.KEY_DOWN,):
            if self.selected is not None:
                self.selected = None if self.selected >= len(choosable) - 1 else self.selected + 1
        elif k == curses.KEY_PPAGE:
            earlier = [d for d in self.days.all_days() if d < self.viewing]
            if earlier:
                self.viewing, self.selected = earlier[-1], None
        elif k == curses.KEY_NPAGE:
            later = [d for d in self.days.all_days() if self.viewing < d <= self.today()]
            self.viewing, self.selected = (later[0] if later else self.today()), None
        elif k == "\x1b":
            self.selected = None
            if self.parse(self.line)[0] == "find" or self.flash:
                self.line, self.cursor, self.flash = "", 0, ""
            self.viewing = self.today()
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if self.selected is not None and self.selected < len(choosable):
                e, _, thread = rows[choosable[self.selected]]
                self.selected = None
                if thread:
                    self.sway.cmd(f"[con_id={thread[0]}] focus")
                else:
                    self.run(e)
            else:
                self.submit()
        elif k in (curses.KEY_DC,) and self.selected is not None and self.selected < len(choosable):
            e, _, thread = rows[choosable[self.selected]]
            found = thread or (self.thread_of(e) if e else None)
            if found:
                self.sway.cmd(f"[con_id={found[0]}] kill")
                self.sway.cmd(f"workspace {PAGE}")
        elif self.selected is not None and isinstance(k, str) and k.isprintable():
            self.selected = None
            self.insert(k)
        elif self.selected is not None:
            pass
        elif k == "\t":
            self.complete()
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if self.cursor:
                self.line = self.line[: self.cursor - 1] + self.line[self.cursor:]
                self.cursor -= 1
        elif k == curses.KEY_DC:
            self.line = self.line[: self.cursor] + self.line[self.cursor + 1:]
        elif k == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif k == curses.KEY_RIGHT:
            self.cursor = min(len(self.line), self.cursor + 1)
        elif k in (curses.KEY_HOME, "\x01"):
            self.cursor = 0
        elif k in (curses.KEY_END, "\x05"):
            self.cursor = len(self.line)
        elif k == "\x15":   # ctrl+u
            self.line, self.cursor = self.line[self.cursor:], 0
        elif k == "\x17":   # ctrl+w
            head = self.line[: self.cursor].rstrip()
            cut = head.rfind(" ") + 1
            self.line, self.cursor = self.line[:cut] + self.line[self.cursor:], cut
        elif isinstance(k, str) and k.isprintable():
            self.insert(k)
        if isinstance(k, str) and k.isprintable():
            self.flash = ""
        self.dirty = True

    def insert(self, s: str) -> None:
        self.line = self.line[: self.cursor] + s + self.line[self.cursor:]
        self.cursor += len(s)

    # --- the loop ------------------------------------------------------------------------
    def loop(self) -> None:
        self.scr.timeout(400)
        while True:
            self.expire()
            while True:
                try:
                    self.on_event(*self.events.get_nowait())
                except queue.Empty:
                    break
            path = self.hint("showfile")
            if path:
                self.days.write(f"code {path}", True, "from Claude")
                if self.editor is not None:
                    self.threads.setdefault(self.editor, {"title": "editor", "app": EDITOR_APP})
                    self.threads[self.editor]["entry"] = self.days.entries(self.today())[-1].key
                    self.sway.cmd(f"[con_id={self.editor}] focus")
                self.dirty = True
            if self.days.changed(self.viewing):
                self.dirty = True
            # A hint goes after 12 s on screen; its clock starts when it is first drawn.
            if self.flash and self.flash == getattr(self, "_flash_seen", "") \
                    and time.monotonic() - self.flash_at > 12:
                self.flash, self.dirty = "", True
            minute = time.strftime("%H:%M")
            if minute != self.minute:
                self.minute, self.dirty = minute, True
            if self.dirty:
                if self.flash != getattr(self, "_flash_seen", ""):
                    self._flash_seen, self.flash_at = self.flash, time.monotonic()
                self.dirty = False
                self.draw()
            try:
                k = self.scr.get_wch()
            except curses.error:
                continue
            if k == curses.KEY_RESIZE:
                self.dirty = True
                continue
            self.key(k)


def main(scr) -> None:
    curses.use_default_colors()
    curses.set_escdelay(25)
    # 0 ink, 1 dim, 2 accent (verbs), 3 an open thread's mark, 4 the chosen row
    curses.init_pair(1, 8 if curses.COLORS >= 16 else curses.COLOR_BLACK, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_BLUE, -1)
    curses.init_pair(4, -1, curses.COLOR_WHITE)
    Page(scr).loop()


if __name__ == "__main__":
    os.environ.setdefault("ESCDELAY", "25")
    curses.wrapper(main)
