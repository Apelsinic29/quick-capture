# Quick Capture

Instant-capture note tray app for Omarchy/Hyprland.

Press the hotkey, type, press Enter. The note is saved as a timestamped
Markdown file and you are back to what you were doing.

## Install

```sh
git clone <this-repo> ~/DEVELOPMENT/quick-capture
chmod +x ~/DEVELOPMENT/quick-capture/quick_capture.py
cp ~/DEVELOPMENT/quick-capture/quick-capture.service \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now quick-capture.service
```

Omarchy ignores `~/.config/autostart/*.desktop` (its entries are `Hidden=true`
stubs), so a systemd **user** unit is the way to autostart.

## Keys

| Key | Action |
| --- | --- |
| SUPER+SHIFT+` | Capture popup |
| SUPER+` | Notes browser (list + editor) |

The SUPER+SHIFT+grave combination is less likely to trigger accidentally during normal typing.

In the capture popup:

| Key | Action |
| --- | --- |
| Enter | Save the note and close |
| Ctrl+Enter | Newline (Enter alone never inserts one) |
| Esc | Discard |

In the notes browser: Ctrl+N new, Ctrl+S save, Ctrl+F jump to the list,
Esc close. Switching notes or windows saves the outgoing note first.

## Notes

Markdown files in `~/Documents/Quick-Capture-Notes/`, named
`YYYY-MM-DD-HHMMSS.md`, newest first in the UI. Two notes saved in the same
second get a `-2` suffix rather than overwriting.

```markdown
# 2026-10-03 15:04

the note text
```

Override the location with `QC_NOTES_DIR`.

## How it works

A tray-only `Gtk.Application` that exports its `StatusNotifierItem` and
`com.canonical.dbusmenu` interfaces directly over D-Bus rather than using
libayatana-appindicator, which never called `RegisterStatusNotifierItem` on this
machine and so left the app invisible.

The tray icon click opens a small GTK popup menu the app draws itself. It
reports `ItemIsMenu=false` on purpose: this shell only builds a DBusMenu handle
for apps whose menu it discovered itself, and logs `No menu present` otherwise.

Two non-Obvious requirements, both easy to get wrong:

- **`Category` must be `ApplicationStatus`** (the spec value). With
  `APPLICATION_STATUS` the shell rejects the item outright — it stays registered
  and hoverable but dead to clicks.
- **`GDK_BACKEND=x11`** is required. Under GTK3's native Wayland backend popup
  placement is ignored and windows land mid-screen.

The hotkey talks to the running tray app over a Unix socket at
`$XDG_RUNTIME_DIR/quick-capture.sock`, so the keybinding never starts a second
copy.

Hyprland bindings live in `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + SHIFT + grave", "Quick Capture", "~/DEVELOPMENT/quick-capture/quick-capture capture", { locked = true })
o.bind("SUPER + grave", "Quick Capture notes", "~/DEVELOPMENT/quick-capture/quick-capture notes", { locked = true })
```

## Tray icon

Pin `quickcapture1` under `omarchy.tray` in `~/.config/omarchy/shell.json`, then
reload the shell with `kill -SIGUSR1 $(pgrep -f quickshell | head -1)`.

## Debugging

Set `QC_LOG=/tmp/qc.log` and read it — the app logs every IPC command, save and
window placement. Shell-side complaints are in `journalctl --user | grep -i
quickcapture`.