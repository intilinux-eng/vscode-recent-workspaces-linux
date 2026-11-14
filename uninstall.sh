#!/bin/sh
# Remove units and script, and unregister every entry this tool published.
set -eu
"$HOME/.local/bin/vscode-recents" clear 2>/dev/null || true
systemctl --user disable --now vscode-recents.path vscode-recents.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/vscode-recents.service" "$HOME/.config/systemd/user/vscode-recents.path" \
      "$HOME/.local/bin/vscode-recents"
systemctl --user daemon-reload
echo "Uninstalled."
