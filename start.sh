#!/usr/bin/env bash
# start.sh — launch the Data Extractor web app (Linux / macOS).
# Run:  bash start.sh   (or:  chmod +x start.sh && ./start.sh)
#
# Dry run (show what would happen, change nothing):
#     bash start.sh --dry-run

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Argument parsing ---------------------------------------------------
DRY_RUN=0
usage() {
    echo "Usage: bash start.sh [--dry-run|-n|--dryrun]"
    echo ""
    echo "  --dry-run, -n, --dryrun   Show what start would do without changing anything."
}

for arg in "$@"; do
    case "$arg" in
        --dry-run|-n|--dryrun)
            DRY_RUN=1
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ ! -x ".venv/bin/python" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] would trigger setup (venv missing: .venv/bin/python)"
        echo "[DRY-RUN] would launch: .venv/bin/python app.py at http://127.0.0.1:5000 (after setup)"
        echo "[DRY-RUN] Dry run complete — nothing was changed."
        exit 0
    fi
    echo "Virtual environment not found - running setup first..."
    echo ""
    bash "$SCRIPT_DIR/setup.sh"
fi

if [ ! -x ".venv/bin/python" ]; then
    echo ""
    echo "Setup did not complete. Please run 'bash setup.sh' and read the messages."
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] venv present, setup skipped"
    echo "[DRY-RUN] would launch: .venv/bin/python app.py at http://127.0.0.1:5000"
    echo "[DRY-RUN] Dry run complete — nothing was changed."
    exit 0
fi

.venv/bin/python app.py
