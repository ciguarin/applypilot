#!/usr/bin/env bash
# ApplyPilot setup script — macOS / Linux
set -e

APPLYPILOT_DIR="$HOME/.applypilot"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "=== ApplyPilot Setup ==="

# ── 1. uv ────────────────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
echo "✓ uv $(uv --version)"

# ── 2. applypilot ────────────────────────────────────────────────────────────
echo "Installing applypilot..."
uv tool install applypilot
export PATH="$HOME/.local/bin:$PATH"
echo "✓ applypilot $(applypilot --version 2>/dev/null || echo installed)"

# ── 3. patches ───────────────────────────────────────────────────────────────
echo "Applying patches..."
APPLYPILOT_PY="$(uv tool dir)/applypilot/bin/python"
DST="$( "$APPLYPILOT_PY" -c 'import applypilot, os; print(os.path.dirname(applypilot.__file__))' )"
SRC="$APPLYPILOT_DIR/patches"
cp "$SRC/scoring/validator.py" "$DST/scoring/validator.py"
cp "$SRC/scoring/tailor.py"    "$DST/scoring/tailor.py"
cp "$SRC/scoring/pdf.py"       "$DST/scoring/pdf.py"
cp "$SRC/cli.py"               "$DST/cli.py"
cp "$SRC/apply/prompt.py"      "$DST/apply/prompt.py"
cp "$SRC/apply/launcher.py"    "$DST/apply/launcher.py"
cp "$SRC/wizard/init.py"       "$DST/wizard/init.py"
echo "✓ Patches applied to $DST"

# ── 4. Python extras (pure-Python, no system deps) ───────────────────────────
echo "Installing Python extras..."
uv pip install --python "$APPLYPILOT_PY" --quiet pypdf
echo "✓ pypdf (PDF-to-text conversion)"

# ── 5. Node.js MCPs (pre-install so sessions never download at runtime) ───────
if command -v npm &>/dev/null; then
    echo "Pre-installing Node.js MCPs..."
    npm install -g --silent @playwright/mcp @codefuturist/email-mcp
    echo "✓ @playwright/mcp + @codefuturist/email-mcp"
else
    echo "  npm not found — Node.js MCPs will download on first use"
    echo "  Install Node.js from https://nodejs.org to pre-cache them"
fi

# ── 6. Browser (Playwright Chromium if no system browser found) ───────────────
_has_browser() {
    local browsers=(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        "/Applications/Chromium.app/Contents/MacOS/Chromium"
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

# ── 7. config templates (only if not already present) ────────────────────────
[[ ! -f "$APPLYPILOT_DIR/.env" ]]          && cp "$APPLYPILOT_DIR/.env.example"               "$APPLYPILOT_DIR/.env"
[[ ! -f "$APPLYPILOT_DIR/profile.json" ]]  && cp "$APPLYPILOT_DIR/config/profile.example.json" "$APPLYPILOT_DIR/profile.json"
[[ ! -f "$APPLYPILOT_DIR/searches.yaml" ]] && cp "$APPLYPILOT_DIR/config/searches.example.yaml" "$APPLYPILOT_DIR/searches.yaml"
echo "✓ Config templates ready"

# ── 8. LaunchAgents (macOS only) ─────────────────────────────────────────────
if [[ "$(uname)" == "Darwin" ]]; then
    mkdir -p "$LAUNCH_AGENTS" "$APPLYPILOT_DIR/logs"

    sed "s|__HOME__|$HOME|g" "$APPLYPILOT_DIR/launchagents/com.applypilot.apply.plist.template" \
        > "$LAUNCH_AGENTS/com.applypilot.apply.plist"
    launchctl unload "$LAUNCH_AGENTS/com.applypilot.apply.plist" 2>/dev/null || true
    launchctl load   "$LAUNCH_AGENTS/com.applypilot.apply.plist"
    echo "✓ Apply daemon installed (runs every 12h)"

    # GitHub README discovery runs natively inside applypilot — n8n no longer needed
fi

echo ""
echo "=== Done! Run: applypilot init ==="
echo ""
