#!/usr/bin/env bash
# setup.sh — one-time setup for Data Extractor (Linux / macOS).
# Creates a Python virtual environment, installs packages, and makes sure
# Ollama + the extraction model are present. Run once:
#     bash setup.sh
# (or:  chmod +x setup.sh && ./setup.sh)
#
# Dry run (show what would happen, change nothing):
#     bash setup.sh --dry-run

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ---- Argument parsing ---------------------------------------------------
DRY_RUN=0
usage() {
    echo "Usage: bash setup.sh [--dry-run|-n|--dryrun]"
    echo ""
    echo "  --dry-run, -n, --dryrun   Show what setup would do without changing anything."
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

echo ""
echo "=============================================="
echo "  Data Extractor - one-time setup"
echo "=============================================="
echo ""
if [ "$DRY_RUN" -eq 1 ]; then
    echo "  DRY RUN — no changes will be made."
    echo ""
fi

# ---- 1. Python ---------------------------------------------------------
PYTHON=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
fi

if [ -z "$PYTHON" ]; then
    echo "[1/3] Python 3 is not installed."
    echo "      Install it from https://www.python.org/downloads/ — on Linux you can"
    echo "      usually run:  sudo apt install python3   (or:  sudo dnf install python3)"
    echo "      then re-run setup.sh."
    exit 1
fi
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] Python found: $PYTHON ($(command -v "$PYTHON"))"
else
    echo "[1/3] Python found ($PYTHON)."
fi

# The app needs Python 3.9+ (it uses newer language syntax).
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))')"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] Python version gate: FAIL (found $PYVER, need >= 3.9)"
    fi
    echo "      ERROR: Python $PYVER is too old. This app needs Python 3.9 or newer (3.11 recommended)."
    echo "      Install Python 3.9+ and re-run setup.sh."
    exit 1
fi
if [ "$DRY_RUN" -eq 1 ]; then
    PYVER="$("$PYTHON" -c 'import sys; print("%d.%d" % (sys.version_info.major, sys.version_info.minor))')"
    echo "[DRY-RUN] Python version gate: PASS (found $PYVER, need >= 3.9)"
fi
echo ""

# ---- 2. Virtual environment + packages ---------------------------------
echo "[2/3] Creating a virtual environment and installing packages..."
if [ ! -x ".venv/bin/python" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        if [ -d ".venv" ]; then
            echo "[DRY-RUN] would run: $PYTHON -m venv .venv — WARNING: .venv exists but looks like a non-Linux/foreign venv, it would be overwritten/mixed"
        else
            echo "[DRY-RUN] would create .venv: $PYTHON -m venv .venv"
        fi
    else
        "$PYTHON" -m venv .venv
    fi
else
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] skip venv creation (venv exists: .venv/bin/python)"
    fi
fi
if [ "$DRY_RUN" -eq 1 ]; then
    echo "[DRY-RUN] would run: .venv/bin/python -m pip install --upgrade pip (network required)"
    echo "[DRY-RUN] would run: .venv/bin/python -m pip install -r requirements.txt (network required)"
else
    .venv/bin/python -m pip install --upgrade pip --quiet
    .venv/bin/python -m pip install -r requirements.txt
    echo "[2/3] Packages installed."
fi
echo ""

# ---- 3. Ollama + model --------------------------------------------------
echo "[3/3] Checking Ollama..."
OLLAMA=""
if command -v ollama >/dev/null 2>&1; then
    OLLAMA="ollama"
fi

if [ -z "$OLLAMA" ]; then
    if command -v brew >/dev/null 2>&1; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[DRY-RUN] would install Ollama via: brew install --cask ollama"
        else
            echo "      Installing Ollama with Homebrew (macOS)..."
            brew install --cask ollama
        fi
    elif [ "$(uname -s)" = "Linux" ]; then
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[DRY-RUN] would install Ollama via: curl -fsSL https://ollama.com/install.sh | sh"
        else
            echo "      Installing Ollama with the official installer..."
            curl -fsSL https://ollama.com/install.sh | sh
        fi
    fi
    if command -v ollama >/dev/null 2>&1; then
        OLLAMA="ollama"
    fi
else
    if [ "$DRY_RUN" -eq 1 ]; then
        OLLAMA_PATH="$(command -v ollama)"
        OLLAMA_VER="$("$OLLAMA" --version 2>&1 | head -n 1 || true)"
        echo "[DRY-RUN] skip Ollama install (found: $OLLAMA_PATH, version: $OLLAMA_VER)"
    fi
fi

MODEL="qwen2.5:7b-instruct"
if [ -n "$OLLAMA" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        if LIST_OUT="$("$OLLAMA" list 2>/dev/null)"; then
            if printf '%s\n' "$LIST_OUT" | grep -qF "$MODEL"; then
                echo "[DRY-RUN] skip model pull ('$MODEL' already present)"
            else
                echo "[DRY-RUN] would run: $OLLAMA pull $MODEL (~8 GB download)"
            fi
        else
            echo "[DRY-RUN] model state unknown (ollama list failed), would attempt: $OLLAMA pull $MODEL"
        fi
    else
        echo "      Downloading the model '$MODEL' (a few GB, one time only)..."
        "$OLLAMA" pull "$MODEL" || {
            echo "      NOTE: model pull reported a problem. Start the Ollama app once"
            echo "      (Linux: 'ollama serve' or 'systemctl start ollama'), then re-run setup.sh."
        }
    fi
else
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] Ollama not installed; would install it first, then run: ollama pull $MODEL (~8 GB download)"
    else
        echo "      Ollama not installed. Install it from https://ollama.com/download,"
        echo "      then re-run setup.sh."
    fi
fi

if [ "$DRY_RUN" -eq 1 ]; then
    echo ""
    echo "=============================================="
    echo "  Dry run complete — nothing was changed."
    echo "  Re-run without --dry-run to apply."
    echo "=============================================="
    exit 0
fi

echo "[3/3] Ollama + model ready."
echo ""
echo "=============================================="
echo "  Setup complete! Run:  bash start.sh"
echo "=============================================="
