#!/bin/sh
# Install for the current user: script in ~/.local/bin, systemd user units, first sync.
set -eu
cd "$(dirname "$0")"
python3 -c 'import gi; gi.require_version("Gio", "2.0")' 2>/dev/null || {
  echo "PyGObject is required (Debian/Ubuntu: sudo apt install python3-gi)"; exit 1; }
install -Dm755 vscode_recents.py "$HOME/.local/bin/vscode-recents"
install -Dm644 systemd/vscode-recents.service "$HOME/.config/systemd/user/vscode-recents.service"
install -Dm644 systemd/vscode-recents.path "$HOME/.config/systemd/user/vscode-recents.path"
systemctl --user daemon-reload
systemctl --user enable --now vscode-recents.path
systemctl --user enable vscode-recents.service
systemctl --user start vscode-recents.service
echo "Installed. Right-click the VS Code icon in the task manager to see recent folders."
"$HOME/.local/bin/vscode-recents" status
