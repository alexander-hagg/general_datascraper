# setup.ps1 — one-time setup for ScrapeLite.
# Creates a Python virtual environment, installs packages, and makes sure
# Ollama + the extraction model are present. Run it once (or double-click
# setup.bat, which runs this file).
#
# Dry run (show what would happen, change nothing):
#     setup.bat --dry-run
#     powershell -File setup.ps1 --dry-run

$ErrorActionPreference = "Stop"

# ---- Argument parsing ---------------------------------------------------
# Parsed manually (rather than via a param block) so that the literal
# "--dry-run" spelling works, in addition to "-DryRun" and "-n".
$DryRun = $false
foreach ($a in $args) {
    switch -Regex ($a) {
        '^(--dry-run|-DryRun|-n|--dryrun)$' { $DryRun = $true }
        default {
            Write-Host "Unknown argument: $a"
            Write-Host "Usage: setup.bat [--dry-run|-DryRun|-n]"
            exit 2
        }
    }
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Refresh-Path {
    $machine = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Write-Host ""
Write-Host "=============================================="
Write-Host "  ScrapeLite - one-time setup"
Write-Host "=============================================="
Write-Host ""
if ($DryRun) {
    Write-Host "  DRY RUN — no changes will be made."
    Write-Host ""
}

# ---- 1. Python ---------------------------------------------------------
$python = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }

$pythonAvailable = ($null -ne $python)

if (-not $pythonAvailable) {
    if ($DryRun) {
        Write-Host "[DRY-RUN] Python not found on PATH."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "[DRY-RUN] would install Python 3.11 with winget: winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements"
            Write-Host "[DRY-RUN] Python version gate: cannot evaluate until Python is installed"
            $python = "python"   # hypothetical, used only for later plan lines
        } else {
            Write-Host "      ERROR: Python is not installed and winget is unavailable."
            Write-Host "      Install it from https://www.python.org/downloads/"
            exit 1
        }
    } else {
        Write-Host "[1/3] Python is not installed."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "      Installing Python 3.11 with winget..."
            winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
            Refresh-Path
            if (Get-Command python -ErrorAction SilentlyContinue) { $python = "python" }
            elseif (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }
        }
        if ($null -eq $python) {
            Write-Host "      ERROR: Python could not be installed automatically."
            Write-Host "      Install it from https://www.python.org/downloads/"
            Write-Host "      (IMPORTANT: tick 'Add python.exe to PATH' during install),"
            Write-Host "      then run setup.bat again."
            exit 1
        }
        $pythonAvailable = $true
    }
}

if ($pythonAvailable) {
    if ($DryRun) {
        Write-Host "[DRY-RUN] Python found: $python ($((Get-Command $python).Source))"
    } else {
        Write-Host "[1/3] Python found ($python)."
    }

    # The app needs Python 3.9+ (it uses newer language syntax). Warn clearly if
    # the installed Python is too old, instead of crashing later with a confusing error.
    $pyVer = (& $python -c "import sys; print('%d.%d' % (sys.version_info.major, sys.version_info.minor))").Trim()
    if ([version]$pyVer -lt [version]"3.9") {
        if ($DryRun) {
            Write-Host "[DRY-RUN] Python version gate: FAIL (found $pyVer, need >= 3.9)"
        }
        Write-Host "      ERROR: Python $pyVer is too old. This app needs Python 3.9 or newer (3.11 recommended)."
        if (-not $DryRun) {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.MessageBox]::Show(
                "Python $pyVer was found, but this app needs Python 3.9 or newer (3.11 recommended).`n`nPlease install Python 3.11 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run setup.bat again.",
                "Python too old",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Error
            ) | Out-Null
        }
        exit 1
    }
    if ($DryRun) {
        Write-Host "[DRY-RUN] Python version gate: PASS (found $pyVer, need >= 3.9)"
    }
}
Write-Host ""

# ---- 2. Virtual environment + packages ---------------------------------
Write-Host "[2/3] Creating a virtual environment and installing packages..."
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    if ($DryRun) {
        if (Test-Path ".venv") {
            Write-Host "[DRY-RUN] would run: $python -m venv .venv — WARNING: .venv exists but looks like a non-Windows/foreign venv, it would be overwritten/mixed"
        } else {
            Write-Host "[DRY-RUN] would create .venv: $python -m venv .venv"
        }
    } else {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      ERROR: could not create the virtual environment."
            exit 1
        }
    }
} else {
    if ($DryRun) {
        Write-Host "[DRY-RUN] skip venv creation (venv exists: .venv\Scripts\python.exe)"
    }
}
if ($DryRun) {
    Write-Host "[DRY-RUN] would run: .venv\Scripts\python.exe -m pip install --upgrade pip (network required)"
    Write-Host "[DRY-RUN] would run: .venv\Scripts\python.exe -m pip install -r requirements.txt (network required)"
} else {
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "      ERROR: package install failed. Check your internet connection and re-run setup.bat."
        exit 1
    }
    Write-Host "[2/3] Packages installed."
}
Write-Host ""

# ---- 3. Ollama + model --------------------------------------------------
Write-Host "[3/3] Checking Ollama..."
$ollama = $null
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    $ollama = "ollama"
} else {
    $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $candidate) { $ollama = $candidate }
}

if ($null -eq $ollama) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        if ($DryRun) {
            Write-Host "[DRY-RUN] would install Ollama with winget: winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements"
        } else {
            Write-Host "      Ollama is not installed. Installing with winget..."
            winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
            Refresh-Path
            if (Get-Command ollama -ErrorAction SilentlyContinue) { $ollama = "ollama" }
            else {
                $candidate = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
                if (Test-Path $candidate) { $ollama = $candidate }
            }
        }
    } else {
        if ($DryRun) {
            Write-Host "[DRY-RUN] Ollama not installed and winget unavailable; would need manual install from https://ollama.com/download"
        } else {
            Write-Host "      Ollama is not installed and winget is unavailable."
            Write-Host "      Install Ollama from https://ollama.com/download and re-run setup.bat."
        }
    }
} else {
    if ($DryRun) {
        $ollamaPath = if ($ollama -eq "ollama") { (Get-Command ollama).Source } else { $ollama }
        $ollamaVer = "unknown"
        try { $ollamaVer = (& $ollama --version 2>&1 | Select-Object -First 1) } catch { $ollamaVer = "unknown" }
        Write-Host "[DRY-RUN] skip Ollama install (found: $ollamaPath, version: $ollamaVer)"
    }
}

$model = "qwen2.5:7b-instruct"
if ($null -ne $ollama) {
    if ($DryRun) {
        $listOk = $true
        $list = $null
        try {
            $list = & $ollama list 2>$null
            if ($LASTEXITCODE -ne 0) { $listOk = $false }
        } catch {
            $listOk = $false
        }
        if (-not $listOk) {
            Write-Host "[DRY-RUN] model state unknown (ollama list failed), would attempt: $ollama pull $model"
        } elseif (($list -join "`n") -match [regex]::Escape($model)) {
            Write-Host "[DRY-RUN] skip model pull ('$model' already present)"
        } else {
            Write-Host "[DRY-RUN] would run: $ollama pull $model (~8 GB download)"
        }
    } else {
        Write-Host "      Downloading the model '$model' (a few GB, one time only)..."
        & $ollama pull $model
        if ($LASTEXITCODE -ne 0) {
            Write-Host "      NOTE: model pull reported a problem. Open the Ollama app once,"
            Write-Host "      then re-run setup.bat (or run:  ollama pull $model)."
        }
    }
} else {
    if ($DryRun) {
        Write-Host "[DRY-RUN] Ollama not installed; would install it first, then run: ollama pull $model (~8 GB download)"
    } else {
        Write-Host "      Skipping model download - install Ollama first, then re-run setup.bat."
    }
}

if ($DryRun) {
    Write-Host ""
    Write-Host "=============================================="
    Write-Host "  Dry run complete — nothing was changed."
    Write-Host "  Re-run without --dry-run to apply."
    Write-Host "=============================================="
    exit 0
}

Write-Host "[3/3] Ollama + model ready."
Write-Host ""
Write-Host "=============================================="
Write-Host "  Setup complete! Double-click start.bat to run."
Write-Host "=============================================="
