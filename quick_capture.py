#!/usr/bin/env python3
"""Quick Capture — instant note tray app for Omarchy/Hyprland.

Tray icon click  -> native shell menu (new note, recent notes, folder, quit)
grave / Fn+`     -> capture popup: type, Enter saves, Ctrl+Enter newline
Notes            -> ~/Documents/Quick-Capture-Notes/YYYY-MM-DD-HHMMSS.md

The SNI item and the dbusmenu are exported by hand rather than via
libayatana-appindicator: on this machine the library never even calls
RegisterStatusNotifierItem, so a tray-only app built on it shows nothing.
See devops/omarchy-tray-plugin for the full rationale.
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

APP_ID = "quickcapture"
BUS_NAME = "com.bigzo.quickcapture"
TRAY_ID = "quickcapture1"
# Object paths. The item path is derived from the bus name by the watcher, but
# we export our own copies so hand-written dbusmenu calls (self-close) have a
# destination.
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/QCFallbackMenu"

XDG_RUNTIME = os.environ.get("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
SOCKET_PATH = os.path.join(XDG_RUNTIME, "quick-capture.sock")

HOME = os.path.expanduser("~")
# The machine has ~/Documents (capital D); a lowercase ~/documents does not
# exist. Honour an override for testing.
NOTES_DIR = os.environ.get("QC_NOTES_DIR") or os.path.join(
    HOME, "Documents", "Quick-Capture-Notes"
)
ICON_DIR = os.path.join(
    HOME, ".local/share/icons/hicolor/scalable/status"
)
ICON_FILE = os.path.join(ICON_DIR, "%s-symbolic.svg" % APP_ID)

LOG_FILE = os.environ.get("QC_LOG", "")
# How many notes the tray menu lists before deferring to the notes window.
MENU_NOTE_LIMIT = int(os.environ.get("QC_MENU_LIMIT", "12"))

# Muted on purpose: the first pass used #e6edf3 on #14181d, which reads as a
# blinding white slab against a dark desktop. Softer foreground, darker field.
FG = "#d2d8de"
DIM = "#98a1ab"
LINE = "#3a4149"
BG = "#2a2f36"
ACCENT = "#7cc4b0"

# !important throughout: the desktop theme's own TextView/Entry rules are
# more specific than a bare type selector, so without this the popup rendered
# with the theme's white background instead of ours.
CSS = b"""
/* Each rule is one selector with plain declarations. GTK3's CSS parser is
   strict: a 4-value padding followed by !important is a parse error, and one
   bad rule makes load_from_data() raise, which killed the whole app at
   startup. Keep declarations simple and test with qc_css_test.py. */
window.qc {
  background-color: @BG;
  color: @FG;
}
window.qc.popup {
  background-color: @BG;
  color: @FG;
  border: 1px solid @LINE;
  border-radius: 10px;
}
.qc-title {
  color: @DIM;
  font-size: 11px;
  padding: 8px 12px 0 12px;
}
.qc-hint {
  color: @DIM;
  font-size: 11px;
  padding: 0 12px 8px 12px;
}
.qc-hint kbd { color: @ACCENT; }
textview.qc {
  background-color: @BG;
  background-image: none;
  color: @FG;
  caret-color: @ACCENT;
  padding: 12px 14px;
  font-family: monospace;
  font-size: 14px;
}
textview.qc:focus {
  background-color: @BG;
  color: @FG;
}
textview.qc:focus-in {
  background-color: @BG;
  color: @FG;
}
textview.qc scrollbar {
  background-color: @BG;
  border: none;
}
textview.qc scrollbar slider {
  background-color: #2b333c;
  border: none;
  border-radius: 4px;
}
.qc-sep {
  color: @DIM;
  font-size: 11px;
  padding: 6px 12px 2px 12px;
}
list.qc {
  background-color: @BG;
  color: @FG;
}
treeview.qc {
  background-color: @BG;
  color: @FG;
}
list.qc row:selected {
  background-color: #1b222a;
  color: @FG;
}
treeview.qc row:selected {
  background-color: #1b222a;
  color: @FG;
}
.qc-row-box { padding: 6px 12px; }
.qc-row-time {
  color: @DIM;
  font-size: 11px;
}
.qc-row-text {
  color: @FG;
  font-size: 13px;
}
button.qc-btn {
  background-image: none;
  background-color: #1a2028;
  color: @FG;
  border: 1px solid @LINE;
  border-radius: 6px;
  padding: 5px 10px;
}
button.qc-btn:hover { background-color: #232c35; }
button.qc-row {
  background-color: transparent;
  border: none;
  padding: 0px;
}
button.qc-row:hover { background-color: #1b222a; }
button.qc-row label {
  padding: 4px 0px;
  color: @FG;
}
"""


def _hex(colour):
    """'#rrggbb' -> Gdk.RGBA, for widget-level colour overrides.

    Gdk.RGBA.parse, not new_with_values: PyGObject exposes no RGBA
    constructor taking components, and an invalid call here takes the whole
    app down at startup.
    """
    rgba = Gdk.RGBA()
    if not rgba.parse(colour):
        log("bad colour %r, falling back to white" % colour)
        rgba.parse("#ffffff")
    return rgba


def app_css():
    """The stylesheet with colour placeholders substituted."""
    css = CSS
    for k, v in (("@BG", BG), ("@FG", FG), ("@DIM", DIM),
                 ("@LINE", LINE), ("@ACCENT", ACCENT)):
        css = css.replace(k.encode(), v.encode())
    return css


def _iface_info(xml):
    """Parse an introspection XML blob into a Gio.DBusInterfaceInfo."""
    node = Gio.DBusNodeInfo.new_for_xml(xml)
    return node.interfaces[0]


def log(*parts):
    if not LOG_FILE:
        return
    try:
        with open(LOG_FILE, "a") as fh:
            fh.write("%.3f %s\n" % (time.time(), " ".join(str(p) for p in parts)))
    except OSError:
        pass


# --------------------------------------------------------------------------
# note storage
# --------------------------------------------------------------------------
# The optional trailing -N is the same-second collision suffix unique_path()
# appends. Without it in the regex those notes are silently hidden from the
# menu and the list, because list_notes() skips anything that does not match.
FNAME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})(?:-\d+)?\.md$")


def ensure_dirs():
    os.makedirs(NOTES_DIR, exist_ok=True)
    os.makedirs(ICON_DIR, exist_ok=True)


def slug(text, limit=44):
    """One-line preview of a note for the menu / list."""
    line = ""
    for raw in text.strip().splitlines():
        if raw.strip():
            line = raw.strip().lstrip("#").strip()
            break
    if not line:
        line = "(empty)"
    line = " ".join(line.split())
    return line[: limit - 1] + "\u2026" if len(line) > limit else line


def list_notes():
    """(path, mtime, preview, iso_stamp) newest first."""
    out = []
    try:
        names = os.listdir(NOTES_DIR)
    except OSError:
        return out
    for name in names:
        if not FNAME_RE.match(name):
            continue
        path = os.path.join(NOTES_DIR, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            body = ""
        try:
            stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        except (OSError, ValueError):
            stamp = ""
        # strip the generated header first, or every preview reads as a date
        out.append((path, mtime, slug(strip_header(body)), stamp))
    out.sort(key=lambda r: r[1], reverse=True)
    return out


def unique_path(now=None):
    """Timestamped filename; append -2, -3 ... if one already exists."""
    now = now or datetime.now()
    base = now.strftime("%Y-%m-%d-%H%M%S")
    path = os.path.join(NOTES_DIR, base + ".md")
    n = 2
    while os.path.exists(path):
        path = os.path.join(NOTES_DIR, "%s-%d.md" % (base, n))
        n += 1
    return path


def note_body(text):
    """Wrap the note text in the timestamped Markdown header."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    body = text.strip("\n")
    return "# %s\n\n%s\n" % (stamp, body)


def strip_header(md):
    """Inverse of note_body: the editable text, without the '# date' line."""
    lines = md.splitlines()
    if lines and lines[0].startswith("# "):
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return "\n".join(rest)
    return md


def write_note(text, path=None):
    """Create (or overwrite) a note. Returns the path."""
    ensure_dirs()
    path = path or unique_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(note_body(text))
    log("saved", path)
    return path


def open_folder():
    Gio.AppInfo.launch_default_for_uri("file://" + NOTES_DIR, None)


# --------------------------------------------------------------------------
# icon
# --------------------------------------------------------------------------
ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
  <g fill="none" stroke="#e6edf3" stroke-width="1.6"
     stroke-linecap="round" stroke-linejoin="round">
    <path d="M5 3.8h9.5L19 8.3v11.9H5z"/>
    <path d="M14.2 3.8v4.7H19"/>
    <path d="M8.2 12.6h7.6M8.2 15.9h5"/>
  </g>
</svg>
"""


def paint_icon():
    ensure_dirs()
    with open(ICON_FILE, "w", encoding="utf-8") as fh:
        fh.write(ICON_SVG)
    subprocess_update_cache()


def subprocess_update_cache():
    for base in (os.environ.get("XDG_DATA_HOME",
                                os.path.join(HOME, ".local/share")),
                 "/usr/share", "/usr/local/share"):
        d = os.path.join(base, "icons/hicolor")
        if os.path.isdir(d):
            subprocess.run(["gtk-update-icon-cache", "-f", "-t", d],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------
# capture popup
# --------------------------------------------------------------------------
class CaptureWindow(Gtk.Window):
    """The instant-save popup. Enter saves, Ctrl+Enter newline, Esc cancels."""

    W, H = 520, 240

    def __init__(self, app):
        super().__init__(title="Quick Capture")
        self.app = app
        # UTILITY, not POPUP_MENU: a POPUP_MENU window is not focusable from
        # another app, so the popup opened while Chromium held focus never
        # received a keystroke and every note silently vanished. UTILITY is a
        # normal focusable top-level that is kept out of the taskbar and
        # window list, which is what this wants.
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_accept_focus(True)
        self.set_focus_on_map(True)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_size_request(self.W, self.H)
        self.get_style_context().add_class("qc")
        self.get_style_context().add_class("popup")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        hint = Gtk.Label(label="Quick Capture \u00b7 Enter saves \u00b7 "
                               "Ctrl+Enter newline \u00b7 Esc cancels")
        hint.set_xalign(0)
        hint.get_style_context().add_class("qc-hint")
        outer.pack_start(hint, False, False, 0)

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_accepts_tab(False)
        self.view.get_style_context().add_class("qc")
        # Widget-level overrides, which no theme stylesheet can beat. The theme
        # paints the TextView background, so CSS alone left the field white;
        # these are what actually turn it grey.
        self._apply_view_colors()

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_shadow_type(Gtk.ShadowType.NONE)
        sw.add(self.view)
        outer.pack_end(sw, True, True, 0)

        self.view.connect("key-press-event", self.on_key)

        self.connect("focus-out-event", self.on_focus_out)
        self.connect("delete-event", lambda *_a: (self.hide(), True)[1])

    def _apply_view_colors(self):
        fg = _hex(FG)
        bg = _hex(BG)
        try:
            self.view.override_color(Gtk.StateFlags.NORMAL, fg)
            self.view.override_color(Gtk.StateFlags.ACTIVE, fg)
            self.view.override_background_color(Gtk.StateFlags.NORMAL, bg)
            self.view.override_background_color(Gtk.StateFlags.ACTIVE, bg)
        except Exception:
            log("override_color failed:", traceback.format_exc())

    def on_key(self, _w, event):
        keyval = event.keyval
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)

        if keyval == Gdk.KEY_Escape:
            self.hide()
            return True

        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if ctrl:
                return False        # Ctrl+Enter: let TextView insert a newline
            self.save()
            return True
        return False

    def on_focus_out(self, *_a):
        # Clicking away discards: a half-typed note should never be saved by
        # accident. Ctrl+Enter or Enter is the only way to keep it.
        self.hide()
        return False

    def save(self):
        buf = self.view.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        buf.set_text("")
        if not text.strip():
            self.hide()
            return None
        path = write_note(text)
        self.app.on_note_saved(path)
        self.hide()
        return path

    def open(self):
        self.view.get_buffer().set_text("")
        self._center_on_screen(pre_map=True)   # must precede mapping on XWayland
        self.show_all()
        self._center_on_screen()
        self.present()
        self.view.grab_focus()
        # present() alone is not enough: when another app (Chromium) holds
        # focus the popup maps but never receives keys, so every Enter is lost
        # and the note never saves. xdg-activate / present_with_time make this
        # window the keyboard focus, which the hotkey depends on.
        self.present_with_time(Gdk.CURRENT_TIME)
        self.view.grab_focus()
        self.app.steal_focus("Quick Capture")

    def _center_on_screen(self, pre_map=False):
        """Centre the capture popup on the active monitor.

        Same two gotchas as the tray menu, both learned the hard way:
        - get_geometry(), not get_workarea() -- with a 1.25 scale the workarea
          reports 1536x864 while the compositor positions in 1920x1080 space.
        - on XWayland the position is only honoured when set BEFORE the
          toplevel is mapped, hence pre_map.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        geo = monitor.get_geometry()
        if pre_map:
            width = self.get_preferred_size()[1].width
            height = self.get_preferred_size()[1].height
        else:
            width = self.get_allocated_width() or self.W
            height = self.get_allocated_height() or self.H
        # a third of the way down reads better than dead centre for a prompt
        x = geo.x + (geo.width - width) // 2
        y = geo.y + (geo.height - height) // 3
        self.move(x, y)
        log("capture place(pre_map=%s): geo=%dx%d -> %d,%d (%dx%d)"
            % (pre_map, geo.width, geo.height, x, y, width, height))


# --------------------------------------------------------------------------
# tray popup menu (own window: this shell never builds a DBusMenu handle)
# --------------------------------------------------------------------------
class TrayMenuWindow(Gtk.Window):
    """The menu shown when the tray icon is clicked.

    This shell's Tray.qml calls openTrayMenu() when ItemIsMenu is true, but it
    only has a DBusMenu handle for apps whose menu it discovered itself -- for
    ours it logs "No menu present" and the click does nothing. The working
    reference on this machine (boxbreathing) reports ItemIsMenu=false and draws
    its own GTK popup, so this does the same.

    Positioning notes (from boxbreathing, which is verified working here):
    a Gtk.WindowType.POPUP never mapped reliably under Hyprland/XWayland, and
    POPUP_MENU/DOCK hints get re-anchored by the compositor and ignore move(),
    so use an undecorated TOPLEVEL with a NORMAL hint.
    """

    BAR_HEIGHT = 44
    ROW_H = 30

    def __init__(self, app):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.NORMAL)
        self.set_accept_focus(True)
        self.set_keep_above(True)
        self.set_app_paintable(True)
        self.get_style_context().add_class("qc")
        self.get_style_context().add_class("popup")

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_border_width(8)
        self.add(root)
        self.root = root

        self.header = Gtk.Label(label="No notes yet")
        self.header.set_xalign(0)
        self.header.get_style_context().add_class("qc-hint")
        root.pack_start(self.header, False, False, 2)

        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.pack_start(self.rows_box, False, False, 4)

        root.pack_start(self._separator(), False, False, 4)
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        root.pack_start(foot, False, False, 0)
        for label, cb in (
            ("New note", lambda: (self.app.open_capture(), self.hide())),
            ("All notes", lambda: (self.app.open_notes(), self.hide())),
            ("Folder", lambda: (open_folder(), self.hide())),
            ("Quit", self.app.quit),
        ):
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("qc-btn")
            b.connect("clicked", lambda _b, f=cb: f())
            foot.pack_start(b, True, True, 0)

        self.connect("key-press-event", self.on_key)
        self.connect("focus-out-event", lambda *_a: (self.hide(), False)[1])
        self.connect("delete-event", lambda *_a: (self.hide(), True)[1])
        self.connect("button-press-event", self.on_outside_click)

    def _separator(self):
        sep = Gtk.Separator()
        sep.get_style_context().add_class("qc-sep")
        return sep

    def _clear(self):
        for child in self.rows_box.get_children():
            self.rows_box.remove(child)

    def refresh(self):
        notes = list_notes()
        self._clear()
        self.header.set_text(
            "%d note%s" % (len(notes), "" if len(notes) == 1 else "s")
            if notes else "No notes yet")
        if not notes:
            lbl = Gtk.Label(label="Nothing captured yet")
            lbl.set_xalign(0)
            lbl.get_style_context().add_class("qc-hint")
            self.rows_box.pack_start(lbl, False, False, 2)
        for path, _mt, text, stamp in notes[:MENU_NOTE_LIMIT]:
            row = Gtk.Button()
            row.set_relief(Gtk.ReliefStyle.NONE)
            row.get_style_context().add_class("qc-row")
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            when = Gtk.Label(label=stamp[5:] if stamp else "")
            when.set_xalign(0)
            when.get_style_context().add_class("qc-row-time")
            body = Gtk.Label(label=text)
            body.set_xalign(0)
            body.set_ellipsize(3)
            body.set_hexpand(True)
            box.pack_start(when, False, False, 0)
            box.pack_start(body, True, True, 0)
            row.add(box)
            row.connect("clicked",
                        lambda _b, p=path: (self.app.open_note(p), self.hide()))
            self.rows_box.pack_start(row, False, False, 0)
        if len(notes) > MENU_NOTE_LIMIT:
            more = Gtk.Label(label="+%d more \u2014 see All notes"
                             % (len(notes) - MENU_NOTE_LIMIT))
            more.set_xalign(0)
            more.get_style_context().add_class("qc-hint")
            self.rows_box.pack_start(more, False, False, 2)
        self.rows_box.show_all()

    def _anchor_under_tray(self, pre_map=False):
        """Park the menu top-right, just under the bar.

        On the Wayland backend the position is only honoured if it is set
        BEFORE the toplevel is mapped; a move() after mapping is silently
        ignored and the WM centres the window instead. So: place from the
        preferred size pre-map, then place again post-map for good measure.
        """
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        # get_geometry(), NOT get_workarea(): with a 1.25 scale the workarea
        # reports 1536x864 while the compositor positions in 1920x1080 space, so
        # a workarea-derived x/y lands the menu in the wrong place entirely.
        # (boxbreathing's working popup uses get_geometry() for this reason.)
        geo = monitor.get_geometry()
        if pre_map:
            natural = self.get_preferred_size()[1]
            width, _height = int(natural.width), int(natural.height)
        else:
            self.get_preferred_size()
            width = self.get_allocated_width()
            if width <= 1:
                width = int(self.get_preferred_size()[1].width)
        margin = 10
        x = geo.x + geo.width - width - margin
        y = geo.y + self.BAR_HEIGHT
        self.move(x, y)
        log("anchor(pre_map=%s): geo=%dx%d+%d+%d width=%d -> move(%d, %d)"
            % (pre_map, geo.width, geo.height, geo.x, geo.y, width, x, y))

    def show_now(self):
        self.refresh()
        self._apply_css()
        # must precede mapping on Wayland or the position is ignored
        self._anchor_under_tray(pre_map=True)
        self.show_all()
        self._anchor_under_tray()
        self.present_with_time(Gdk.CURRENT_TIME)

    def _apply_css(self):
        # own provider, like boxbreathing: a provider applied only to this
        # window's style context does not reach its child widgets
        provider = Gtk.CssProvider()
        provider.load_from_data(app_css())   # module-level function, not a method
        self.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def on_outside_click(self, _w, event):
        # a click anywhere that is not one of our own rows dismisses the menu
        if event.button == 1:
            self.hide()
        return False

    def on_key(self, _w, event):
        if event.keyval in (Gdk.KEY_Escape, Gdk.KEY_KP_Escape):
            self.hide()
            return True
        if event.keyval in (Gdk.KEY_n, Gdk.KEY_N):
            self.hide()
            self.app.open_capture()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.hide()
            self.app.open_notes()
            return True
        return False


# --------------------------------------------------------------------------
# notes window: list + editor
# --------------------------------------------------------------------------
class NotesWindow(Gtk.Window):
    """Scrollable note list; editing happens in place on the right pane."""

    def __init__(self, app):
        super().__init__(title="Quick Capture Notes")
        self.app = app
        self.set_default_size(720, 480)
        self.get_style_context().add_class("qc")
        self.current = None            # path being edited
        self._loading = False          # guard against our own buffer signals

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_border_width(8)   # Gtk.Box has no set_margin()
        outer.pack_start(bar, False, False, 0)

        new_btn = Gtk.Button(label="New note")
        new_btn.get_style_context().add_class("qc-btn")
        new_btn.connect("clicked", lambda *_a: self.app.open_capture())
        bar.pack_start(new_btn, False, False, 0)

        folder_btn = Gtk.Button(label="Open folder")
        folder_btn.get_style_context().add_class("qc-btn")
        folder_btn.connect("clicked", lambda *_a: open_folder())
        bar.pack_start(folder_btn, False, False, 0)

        self.status = Gtk.Label(label="")
        self.status.set_xalign(1)
        self.status.set_hexpand(True)
        self.status.get_style_context().add_class("qc-hint")
        bar.pack_end(self.status, True, True, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.pack_start(paned, True, True, 0)

        # --- list pane ---
        self.store = Gtk.ListStore(str, str, str, str)  # path, time, text, iso
        view = Gtk.TreeView(model=self.store)
        view.set_headers_visible(False)
        view.set_enable_search(True)
        view.set_search_column(2)
        view.get_style_context().add_class("qc")
        view.get_selection().connect("changed", self.on_select)

        # set_row_activatable lives on the TreeSelection, not the TreeView
        view.get_selection().set_mode(Gtk.SelectionMode.BROWSE)
        view.connect("row-activated", lambda *_a: self.focus_editor())

        # Column 1 is the time: fixed width, no ellipsize, so a long HH:MM
        # cannot squeeze itself down to "1...". Column 2 takes the rest and is
        # the only one that ellipsizes.
        for idx in (1, 2):
            cr = Gtk.CellRendererText()
            if idx == 1:
                cr.set_property("xalign", 1.0)
                # keep the column object: get_column() indexes the *appended*
                # columns, so asking for index 1 before the append is None.
                time_col = Gtk.TreeViewColumn("time", cr, text=1)
                time_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
                time_col.set_fixed_width(58)
                view.append_column(time_col)
            else:
                cr.set_property("ellipsize", 3)
                view.append_column(
                    Gtk.TreeViewColumn("text", cr, text=2))

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(view)
        paned.add1(sw)
        self.view = view

        # --- editor pane ---
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.title = Gtk.Label(label="No note selected")
        self.title.set_xalign(0)
        self.title.set_margin_top(8)
        self.title.set_margin_bottom(4)
        self.title.set_margin_start(12)
        self.title.get_style_context().add_class("qc-title")
        right.pack_start(self.title, False, False, 0)

        self.editor = Gtk.TextView()
        self.editor.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.editor.get_style_context().add_class("qc")
        # same widget-level overrides as the capture popup: the theme's
        # TextView background otherwise renders as a bright white slab
        fg, bg = _hex(FG), _hex(BG)
        for state in (Gtk.StateFlags.NORMAL, Gtk.StateFlags.ACTIVE):
            self.editor.override_color(state, fg)
            self.editor.override_background_color(state, bg)
        esw = Gtk.ScrolledWindow()
        esw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        esw.set_shadow_type(Gtk.ShadowType.NONE)
        esw.add(self.editor)
        right.pack_end(esw, True, True, 0)
        paned.add2(right)
        paned.set_position(280)

        self.editor.get_buffer().connect("changed", self.on_edited)
        self.editor.connect("key-press-event", self.on_editor_key)

        # Explicit key handling rather than a Gtk.AccelGroup: an accel group
        # on a Gtk.Window only fires while the window is the active toplevel,
        # and Ctrl+N was silently doing nothing when the notes window had
        # focus. Unhandled key presses reach the window's key-press-event
        # handler regardless, so this is the reliable form.
        self.connect("key-press-event", self.on_window_key)

        self.connect("delete-event", lambda *_a: (self.hide(), False)[1])

    def on_window_key(self, _w, event):
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if not ctrl:
            return False
        key = event.keyval
        if key in (Gdk.KEY_n, Gdk.KEY_N):
            self.app.open_capture()
            return True
        if key in (Gdk.KEY_s, Gdk.KEY_S):
            self.save_current()
            return True
        if key in (Gdk.KEY_f, Gdk.KEY_F):
            self.view.grab_focus()
            return True
        return False

    # --- list ---
    def refresh(self, keep=None):
        keep = keep or self.current
        sel = self.store.get_iter_first()
        target = None
        self.store.clear()
        for path, _mtime, text, stamp in list_notes():
            it = Gtk.TreeStoreIter = self.store.append(
                (path, stamp[11:] if stamp else "", text, stamp)
            )
            if path == keep:
                target = it
        if target is not None:
            self.view.get_selection().select_iter(target)
            self.on_edited()          # re-derive the title/selection state
        elif keep is None and len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_first())
        n = len(self.store)
        self.status.set_text("%d note%s" % (n, "" if n == 1 else "s"))

    def on_select(self, sel):
        model, it = sel.get_selected()
        if it is None:
            return
        path = model[it][0]
        if path == self.current:
            return
        # Save the outgoing note before switching so edits are never lost.
        if self.current and getattr(self, "_loading", False) is False:
            self.save_current(quiet=True)
        self.load_note(path)

    def load_note(self, path):
        self.current = path
        self._loading = True
        try:
            with open(path, encoding="utf-8") as fh:
                md = fh.read()
        except OSError:
            md = ""
        buf = self.editor.get_buffer()
        buf.set_text(strip_header(md))
        self._loading = False
        self.title.set_text(os.path.basename(path))
        self.dirty = False

    # --- editing ---
    def on_edited(self, *_a):
        if getattr(self, "_loading", False):
            return
        self.dirty = True
        if self.current:
            self.title.set_text(os.path.basename(self.current) + " \u2022")

    def on_editor_key(self, _w, event):
        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if (event.keyval in (Gdk.KEY_s, Gdk.KEY_S)
                and event.state & Gdk.ModifierType.CONTROL_MASK):
            self.save_current()
            return True
        return False

    def save_current(self, quiet=False, *_a):
        if not self.current or not getattr(self, "dirty", False):
            if not quiet:
                self.status.set_text("nothing to save")
            return False
        buf = self.editor.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        write_note(text, self.current)
        self.dirty = False
        self.title.set_text(os.path.basename(self.current))
        self.refresh(keep=self.current)
        self.app.on_note_saved(self.current)
        return True

    def focus_editor(self):
        self.editor.grab_focus()

    def show_now(self):
        self.refresh()
        self.show_all()
        self.present()
        # Keyboard focus must land in the editor, not the list: opening the
        # window and typing has to edit the note, otherwise the keystrokes
        # land on the TreeView and are silently lost.
        if len(self.store):
            self.focus_editor()
        else:
            self.view.grab_focus()
        # Same reason as the capture popup: GTK focus alone is not enough, the
        # compositor must hand the window the keyboard or every keystroke goes
        # to whichever app had focus before.
        self.app.steal_focus("Quick Capture Notes")


# --------------------------------------------------------------------------
# StatusNotifierItem + com.canonical.dbusmenu, exported directly
# --------------------------------------------------------------------------
SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="ToolTip" type="sa(iiay)ss" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <!-- TWO args only: quickshell calls activate(x, y). Declaring the spec's
         optional third timestamp arg makes every left click a signature
         mismatch, so the call is rejected before the handler runs. -->
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate"/>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="b" direction="in"/>
    </method>
  </interface>
</node>
"""

DBUSMENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="properties" type="a{ia{sv}}" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
      <arg name="id" type="u" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
  </interface>
</node>
"""

# Menu item ids.
ID_NEW = 1
ID_NOTES = 2
ID_FOLDER = 3
ID_QUIT = 4
ID_NOTE_BASE = 100     # note rows are 100 + index


class TrayIcon(Gtk.Application):
    """The SNI object. A Gtk.Application subclass only for typing clarity."""

    def __init__(self, app):
        super().__init__(application_id="com.bigzo.quickcapture.tray",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.app = app
        self.conn = None
        self._last_tooltip = None

    # --- menu contents ---
    def _menu_items(self):
        notes = list_notes()
        shown = notes[:MENU_NOTE_LIMIT]
        rows = [(ID_NEW, "New note", "item", ("capture", None))]
        more = len(notes) - len(shown)
        if more > 0:
            rows.append(
                (ID_NOTES, "All notes (%d) \u2026" % len(notes), "item",
                 ("notes", None)))
        else:
            rows.append((ID_NOTES, "All notes", "item", ("notes", None)))
        rows.append((90, "", "separator", None))
        if shown:
            for i, (_path, _mt, text, stamp) in enumerate(shown):
                when = stamp[5:] if stamp else ""
                rows.append((ID_NOTE_BASE + i,
                             "%s  %s" % (when, text), "item",
                             ("open", _path)))
            rows.append((91, "", "separator", None))
        else:
            rows.append((92, "No notes yet", "item", None))
            rows.append((91, "", "separator", None))
        rows.append((ID_FOLDER, "Open notes folder", "item",
                     ("folder", None)))
        rows.append((93, "", "separator", None))
        rows.append((ID_QUIT, "Quit", "item", ("quit", None)))
        return rows

    # --- dbusmenu plumbing ---
    LAYOUT_TYPE = GLib.VariantType.new("(u(ia{sv}av))")
    _revision = 1

    @classmethod
    def _menu_nodes(cls, children):
        parts = []
        for row_id, label, kind, _action in children:
            if kind == "separator":
                props = ['"type": <"separator">', '"visible": <true>']
            else:
                esc = label.replace("\\", "\\\\").replace('"', '\\"')
                # Every row enabled: the shell renders disabled rows at 45%
                # opacity. Rows with no action are harmless no-ops on click.
                props = ['"label": <"%s">' % esc, '"visible": <true>',
                         '"enabled": <true>']
            parts.append(
                "<(%d, @a{sv} {%s}, @av [])>" % (row_id, ", ".join(props)))
        return ", ".join(parts)

    @classmethod
    def _menu_layout(cls, node_id, children):
        # (ia{sv}av) cannot be built from Python tuples here (empty child array
        # raises KeyError inside the override), so parse GVariant text.
        text = "(1, (%d, @a{sv} {}, @av [%s]))" % (
            node_id, cls._menu_nodes(children))
        return GLib.Variant.parse(cls.LAYOUT_TYPE, text, None, None)

    def _bump_layout(self):
        self._revision += 1
        try:
            self.conn.emit_signal(
                None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)))
        except GLib.Error as exc:
            log("LayoutUpdated failed:", exc.message)

    def _menu_get_property(self, _c, _s, _p, _i, name):
        """Property getter for the dbusmenu interface (Version, Status, ...).

        register_object() needs this: the shell reads the menu interface's
        Version/Status properties, and passing None for it leaves the object
        half-registered -- the shell then logs "No menu present" and clicking
        the icon does nothing.
        """
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _menu_method_call(self, _c, _s, _p, _i, method, params, inv):
        try:
            if method == "GetLayout":
                rows = self._menu_items()
                inv.return_value(self._menu_layout(0, rows))
            elif method == "Event":
                item_id, event_id, _data, _ts = params.unpack()
                if event_id in ("clicked", "pressed"):
                    self._menu_trigger(item_id)
                inv.return_value(GLib.Variant("(u)", (0,)))
            elif method == "AboutToShow":
                self._bump_layout()
                inv.return_value(GLib.Variant("(b)", (False,)))
            elif method == "GetGroupProperties":
                inv.return_value(GLib.Variant("(a{ia{sv}})", ({},)))
            elif method == "GetProperty":
                inv.return_value(GLib.Variant("(v)", (
                    GLib.Variant("u", 3) if params.unpack()[1] == "Version"
                    else GLib.Variant("s", "ltr"),)))
            else:
                inv.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception:
            log("dbusmenu raised:", traceback.format_exc())
            try:
                inv.return_value(None)
            except Exception:
                pass

    def _menu_trigger(self, item_id):
        log("menu click", item_id)
        for row in self._menu_items():
            if row[0] != item_id:
                continue
            action = row[3]
            if action is None:
                return
            what, value = action
            if what == "capture":
                self.app.open_capture()
            elif what == "notes":
                self.app.open_notes()
            elif what == "open":
                self.app.open_note(value)
            elif what == "folder":
                open_folder()
            elif what == "quit":
                self.app.quit()
            return

    # --- SNI properties ---
    def _sni_get_property(self, _c, _s, _p, _i, name):
        if name == "Category":
            # MUST be the spec value "ApplicationStatus", not
            # "APPLICATION_STATUS": quickshell validates it and rejects the
            # whole item with
            #   'Nonconformant StatusNotifierItem Category: APPLICATION_STATUS'
            # which leaves the item registered but with no menu and no clicks.
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", TRAY_ID)
        if name == "Title":
            return GLib.Variant("s", "Quick Capture")
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "ItemIsMenu":
            # MUST be false on this shell. With true, Tray.qml routes the left
            # click to openTrayMenu(), which needs a DBusMenu handle the shell
            # never builds for us; it then logs "No menu present" and the click
            # does nothing. With false, the left click arrives as Activate() and
            # we show our own window. (boxbreathing, the working reference here,
            # also reports false and renders its own GTK popup.)
            return GLib.Variant("b", False)
        if name == "IconName":
            return GLib.Variant("s", "%s-symbolic" % APP_ID)
        if name == "ToolTip":
            n = len(list_notes())
            text = "Quick Capture \u00b7 %d note%s" % (n, "" if n == 1 else "s")
            return GLib.Variant(
                "(sa(iiay)ss)", (APP_ID, [], text, "Quick Capture"))
        return None

    def _sni_method_call(self, _c, _s, _p, _i, method, params, inv):
        try:
            if method == "Activate":
                # quickshell sends (x, y) with no click count; one debounced
                # click opens the capture popup (the native menu is the
                # right-click / scroll path).
                try:
                    x, y = params.unpack()
                except (TypeError, ValueError):
                    x = y = 0
                self.app.on_icon_click(int(x), int(y))
            elif method == "ContextMenu":
                self.app.open_menu()
            elif method == "SecondaryActivate":
                self.app.open_menu()
            elif method == "Scroll":
                delta, _orient = params.unpack()
                if delta > 0:
                    self.app.open_menu()
            inv.return_value(None)
        except Exception:
            log("SNI handler raised:", traceback.format_exc())
            inv.return_value(None)

    # --- watcher registration ---
    WATCHER_NAME = "org.kde.StatusNotifierWatcher"
    WATCHER_PATH = "/StatusNotifierWatcher"

    def register_with_watcher(self, bus_name):
        self.bus_name = bus_name
        self.entry = "%s/StatusNotifierItem" % bus_name
        GLib.timeout_add_seconds(2, self.ensure_registered)
        GLib.timeout_add_seconds(15, self.ensure_registered)

    def _watcher_call(self, interface, method, params):
        return self.conn.call_sync(
            self.WATCHER_NAME, self.WATCHER_PATH, interface, method, params,
            None, Gio.DBusCallFlags.NONE, 4000, None)

    def ensure_registered(self):
        try:
            try:
                reply = self._watcher_call(
                    "org.freedesktop.DBus.Properties", "Get",
                    GLib.Variant("(ss)", (self.WATCHER_NAME,
                                          "RegisteredStatusNotifierItems")))
                names = [str(v) for v in reply.unpack()[0]]
                if self.entry in names:
                    return True
            except GLib.Error:
                pass
            self._watcher_call(
                self.WATCHER_NAME, "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self.bus_name,)))
            log("registered with watcher as", self.bus_name)
        except GLib.Error as exc:
            log("watcher registration failed:", exc.message)
        return True

    def refresh_tooltip(self):
        n = len(list_notes())
        text = "Quick Capture \u00b7 %d note%s" % (n, "" if n == 1 else "s")
        if text == self._last_tooltip:
            return
        self._last_tooltip = text
        try:
            self.conn.emit_signal(None, ITEM_PATH,
                                  "org.kde.StatusNotifierItem", "NewToolTip",
                                  None)
        except GLib.Error:
            pass


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=BUS_NAME,
                         flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        ensure_dirs()
        paint_icon()

        self.capture = CaptureWindow(self)
        self.notes = NotesWindow(self)
        self.tray_menu = TrayMenuWindow(self)

        # dark, minimal styling shared by both windows
        provider = Gtk.CssProvider()
        provider.load_from_data(app_css())
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.conn = Gio.bus_get_sync(
            Gio.BusType.SESSION, None)
        # SNI + dbusmenu are exported by hand: libayatana-appindicator on this
        # machine never calls RegisterStatusNotifierItem, so a tray-only app
        # built on it is invisible. Both handler sets live on TrayIcon, so the
        # objects can only be exported once that instance exists.
        #
        # register_object() wants a parsed Gio.DBusInterfaceInfo (it takes
        # object_path first, then the interface, then the closures), so the
        # XML is parsed rather than passed as a string.
        self.tray = TrayIcon(self)
        self.tray.conn = self.conn
        sni_iface = _iface_info(SNI_XML)
        menu_iface = _iface_info(DBUSMENU_XML)
        self.conn.register_object(
            ITEM_PATH, sni_iface, self.tray._sni_method_call,
            self.tray._sni_get_property, None)
        self.conn.register_object(
            MENU_PATH, menu_iface, self.tray._menu_method_call,
            self.tray._menu_get_property, None)
        # Also export com.canonical.dbusmenu on the ITEM's own path. The shell
        # builds its .menu handle from the object it registered for the item
        # (the bus name + /StatusNotifierItem), and Tray.qml's openTrayMenu
        # bails out to a no-op when item.menu is null. Exporting on both paths
        # costs nothing and covers either lookup.
        self.conn.register_object(
            ITEM_PATH, menu_iface, self.tray._menu_method_call,
            self.tray._menu_get_property, None)
        self.tray.register_with_watcher(BUS_NAME)

        # Left click: debounce so a double-click is not two popups.
        self._last_click = 0.0

        self.sock = serve_socket(self)
        self.hold()

    def steal_focus(self, title):
        """Make sure this window really holds the keyboard.

        Under Wayland a client cannot raise and focus itself: present() maps
        the window but the compositor leaves focus wherever it was, so the
        popup appeared to work while every keystroke went to the previously
        focused app and no note was ever saved. Hyprland's own dispatcher is
        the supported way to do this, so ask it to focus the window by title
        once it has mapped.
        """
        def go():
            try:
                subprocess.run(
                    ["hyprctl", "dispatch", "focuswindow",
                     "title:^%s$" % re.escape(title)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=3)
                log("steal_focus ->", title)
            except Exception:
                log("steal_focus failed:", traceback.format_exc())
            return False

        # after the map request has been processed by the compositor
        GLib.timeout_add(120, go)

    # --- tray click ---
    def on_icon_click(self, x, y):
        """Left click when the shell routes it here (middle/scroll use the
        other handlers).

        The shell sends Activate for a left click (ItemIsMenu is false because
        this shell builds no DBusMenu handle for us), so this is where a click
        on the icon lands. Show the menu -- clicking an icon should list the
        notes -- and toggle it so a second click closes it again.

        This used to be a debounce that armed on the first click and only acted
        on a second click within 350ms, so a single ordinary click did nothing
        at all.
        """
        self.open_menu()

    # --- windows ---
    def open_menu(self):
        if self.tray_menu.get_visible():
            self.tray_menu.hide()
            return
        self.capture.hide()
        self.notes.hide()
        self.tray_menu.show_now()
        self.steal_focus("Quick Capture")

    def open_capture(self):
        self.tray_menu.hide()
        self.notes.hide()
        self.capture.open()

    def open_notes(self):
        self.tray_menu.hide()
        self.capture.hide()
        self.notes.show_now()

    def open_note(self, path):
        self.tray_menu.hide()
        self.capture.hide()
        self.notes.show_now()
        # find the row for this path and select it
        for row in self.notes.store:
            if row[0] == path:
                self.notes.view.get_selection().select_iter(row.iter)
                self.notes.load_note(path)
                break
        self.notes.focus_editor()

    def on_note_saved(self, path):
        self.tray.refresh_tooltip()
        self.tray._bump_layout()
        self.notes.refresh(keep=self.notes.current)
        log("note saved:", path)

    # --- hotkey IPC ---
    def on_socket_command(self, line):
        cmd = line.strip()
        log("ipc:", cmd)
        if cmd in ("capture", "toggle"):
            self.open_capture()
        elif cmd == "notes":
            self.open_notes()
        elif cmd == "quit":
            self.quit()

    def do_shutdown(self):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        Gtk.Application.do_shutdown(self)   # required, or GApplication warns


# --------------------------------------------------------------------------
# IPC socket: this is what the Hyprland binding calls
# --------------------------------------------------------------------------
def serve_socket(app):
    """Unix socket so a hotkey binding can poke the running tray app."""
    try:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
    except OSError:
        pass

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)
    srv.listen(4)

    def loop():
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            try:
                data = conn.recv(256).decode("utf-8", "replace")
                GLib.idle_add(app.on_socket_command, data)
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def send_command(cmd):
    """Client side: used by the `quick-capture` helper script."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(SOCKET_PATH)
    s.sendall(cmd.encode())
    s.close()
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("capture", "notes", "quit"):
        return send_command(sys.argv[1])
    log("=== quick-capture starting ===")
    app = App()
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())