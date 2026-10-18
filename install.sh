#!/bin/sh
set -eu

SERVICE_NAME=ollama-load.service
SERVICE_FILE=ollama-load.service
SCRIPT_FILE=ollama_load_service.py
GUI_SCRIPT_FILE=ollama_load_gui.py
OS_SCRIPT_FILE=os-load.py
OLLAMA_DESKTOP_FILE=ollama-load.desktop
OS_DESKTOP_FILE=os-load.desktop
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
USER_INSTALL=0

if [ "${1:-}" = "--user" ]; then
    USER_INSTALL=1
elif [ "$#" -gt 0 ]; then
    echo "Usage: $0 [--user]" >&2
    exit 1
fi

if [ "$USER_INSTALL" -eq 1 ]; then
    INSTALL_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/ollama-load
    APPLICATIONS_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/applications
    PRIVILEGE=
else
    INSTALL_DIR=/opt/ollama-load
    APPLICATIONS_DIR=/usr/share/applications
fi

if [ "$USER_INSTALL" -eq 1 ]; then
    :
elif [ "$(id -u)" -eq 0 ]; then
    PRIVILEGE=
elif command -v sudo >/dev/null 2>&1; then
    PRIVILEGE=sudo
else
    echo "Error: this script must be run as root or with sudo available." >&2
    exit 1
fi

for f in "$SCRIPT_DIR/$GUI_SCRIPT_FILE" "$SCRIPT_DIR/$OS_SCRIPT_FILE" \
    "$SCRIPT_DIR/$OLLAMA_DESKTOP_FILE" "$SCRIPT_DIR/$OS_DESKTOP_FILE"; do
    if [ ! -f "$f" ]; then
        echo "Error: required file '$f' not found." >&2
        exit 1
    fi
done
if [ "$USER_INSTALL" -eq 0 ]; then
    for f in "$SCRIPT_DIR/$SCRIPT_FILE" "$SCRIPT_DIR/$SERVICE_FILE"; do
        if [ ! -f "$f" ]; then
            echo "Error: required file '$f' not found." >&2
            exit 1
        fi
    done
fi

$PRIVILEGE install -d "$INSTALL_DIR"
if [ "$USER_INSTALL" -eq 0 ]; then
    $PRIVILEGE install -m 0755 "$SCRIPT_DIR/$SCRIPT_FILE" "$INSTALL_DIR/"
fi
$PRIVILEGE install -m 0755 "$SCRIPT_DIR/$GUI_SCRIPT_FILE" "$INSTALL_DIR/"
$PRIVILEGE install -m 0755 "$SCRIPT_DIR/$OS_SCRIPT_FILE" "$INSTALL_DIR/"
$PRIVILEGE install -d "$APPLICATIONS_DIR"
if [ "$USER_INSTALL" -eq 1 ]; then
    for desktop_file in "$OLLAMA_DESKTOP_FILE" "$OS_DESKTOP_FILE"; do
        temporary_file=$(mktemp)
        trap 'rm -f "$temporary_file"' EXIT HUP INT TERM
        sed "s|/opt/ollama-load|$INSTALL_DIR|g" "$SCRIPT_DIR/$desktop_file" >"$temporary_file"
        install -m 0644 "$temporary_file" "$APPLICATIONS_DIR/$desktop_file"
        rm -f "$temporary_file"
        trap - EXIT HUP INT TERM
    done
    echo "Installed desktop applications for the current user."
else
    $PRIVILEGE install -m 0644 "$SCRIPT_DIR/$SERVICE_FILE" /etc/systemd/system/
    $PRIVILEGE install -m 0644 "$SCRIPT_DIR/$OLLAMA_DESKTOP_FILE" "$APPLICATIONS_DIR/"
    $PRIVILEGE install -m 0644 "$SCRIPT_DIR/$OS_DESKTOP_FILE" "$APPLICATIONS_DIR/"
    $PRIVILEGE systemctl daemon-reload
    $PRIVILEGE systemctl enable --now "$SERVICE_NAME"
    echo "Installed and started $SERVICE_NAME."
fi
