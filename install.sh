#!/usr/bin/env bash
# ApplyPilot setup script — macOS
# Usage: curl -fsSL https://raw.githubusercontent.com/ciguarin/applypilot/main/install.sh | bash
set -e

APPLYPILOT_DIR="$HOME/.applypilot"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
REPO="https://github.com/ciguarin/applypilot"

echo "=== ApplyPilot Setup ==="

# ── 1. uv ─────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "✓ uv $(uv --version)"

# ── 2. applypilot (install from git) ──────────────────────────────────────────
echo "Installing applypilot..."
uv tool install "git+$REPO@v1" --force
export PATH="$HOME/.local/bin:$PATH"
echo "✓ applypilot $(applypilot --version 2>/dev/null || echo installed)"

# Locate installed package for bundled assets
APPLYPILOT_PY="$(uv tool dir)/applypilot/bin/python"
PKG="$("$APPLYPILOT_PY" -c 'import applypilot, os; print(os.path.dirname(applypilot.__file__))')"

# ── 3. Discovery dependencies (jobspy) ───────────────────────────────────────
echo "Installing discovery dependencies..."
"$APPLYPILOT_PY" -m pip install --no-deps python-jobspy --quiet
"$APPLYPILOT_PY" -m pip install pydantic tls-client requests markdownify regex --quiet
echo "✓ python-jobspy installed"

# ── 4. Node.js MCPs (pre-install so sessions never download at runtime) ────────
if command -v npm &>/dev/null; then
    echo "Pre-installing Node.js MCPs..."
    npm install -g --silent @playwright/mcp @codefuturist/email-mcp
    echo "✓ @playwright/mcp + @codefuturist/email-mcp"
else
    echo "  npm not found — Node.js MCPs will download on first use"
    echo "  Install Node.js from https://nodejs.org to pre-cache them"
fi

# ── 4. Browser (Playwright Chromium if no system browser found) ───────────────
_has_browser() {
    local browsers=(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        "/Applications/Chromium.app/Contents/MacOS/Chromium"
        "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi"
        "/Applications/Arc.app/Contents/MacOS/Arc"
    )
    for b in "${browsers[@]}"; do
        [[ -f "$b" ]] && return 0
    done
    for cmd in google-chrome google-chrome-stable chromium-browser chromium brave-browser; do
        command -v "$cmd" &>/dev/null && return 0
    done
    return 1
}

if _has_browser; then
    echo "✓ System browser detected"
elif command -v npx &>/dev/null; then
    echo "No system browser found — downloading Playwright Chromium (~300MB)..."
    npx --yes playwright install chromium
    echo "✓ Playwright Chromium installed"
else
    echo "  No browser found and npx unavailable — install Chrome or Node.js"
fi

# ── 5. Data directory + config templates ──────────────────────────────────────
mkdir -p "$APPLYPILOT_DIR/logs"

[[ ! -f "$APPLYPILOT_DIR/.env" ]]          && cp "$PKG/config/.env.example"        "$APPLYPILOT_DIR/.env"
[[ ! -f "$APPLYPILOT_DIR/profile.json" ]]  && cp "$PKG/config/profile.example.json" "$APPLYPILOT_DIR/profile.json"
[[ ! -f "$APPLYPILOT_DIR/searches.yaml" ]] && cp "$PKG/config/searches.example.yaml" "$APPLYPILOT_DIR/searches.yaml"
echo "✓ Config templates ready"

# ── 6. Daemon script ──────────────────────────────────────────────────────────
cp "$PKG/scripts/apply_daemon.sh" "$APPLYPILOT_DIR/apply_daemon.sh"
chmod +x "$APPLYPILOT_DIR/apply_daemon.sh"

# ── 7. LaunchAgent (macOS only) ───────────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
    mkdir -p "$LAUNCH_AGENTS"

    sed "s|__HOME__|$HOME|g" "$PKG/scripts/launchagents/com.applypilot.apply.plist.template" \
        > "$LAUNCH_AGENTS/com.applypilot.apply.plist"
    launchctl unload "$LAUNCH_AGENTS/com.applypilot.apply.plist" 2>/dev/null || true
    launchctl load   "$LAUNCH_AGENTS/com.applypilot.apply.plist"
    if launchctl list com.applypilot.apply &>/dev/null; then
        echo "✓ Apply daemon loaded (runs at 08:00 and 20:00 daily)"
    else
        echo "WARN: Daemon not loaded — run 'applypilot doctor' to diagnose"
    fi
fi

echo ""
echo "=== Done! Run: applypilot init ==="
echo ""
