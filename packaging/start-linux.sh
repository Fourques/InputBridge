#!/bin/sh
# Launch the extracted development bundle from an existing graphical X11 session.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
python="$root/.venv/bin/python"
profile="${XDG_DATA_HOME:-$HOME/.local/share}/inputbridge"
if [ ! -x "$python" ]; then
    printf 'Set up the private Python environment first; see docs/Installation.md.\n' >&2
    exit 1
fi
cd "$root"
exec "$python" -m inputbridge --profile "$profile" "$@"
