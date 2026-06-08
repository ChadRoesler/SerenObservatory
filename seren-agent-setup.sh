#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  seren-agent-setup.sh  -  one-shot SerenAgent installer (Linux)
#
#  Rip it and win. This script:
#    1. Finds a usable Python (3.10-3.12)
#    2. Makes a clean venv at ~/seren-venvs/agent  (no pip wrestling)
#    3. Installs seren-agent from the latest GitHub release (or a
#       local .whl you hand it)
#    4. Drops a double-clickable run-seren-agent.sh launcher
#    5. (optional) installs a systemd service so it starts on boot
#
#  The defaults are SAFE: binds 0.0.0.0 on port 7777 (Seren cluster
#  convention). Add --token or --gen-token to require auth.
#
#  USAGE
#    bash seren-agent-setup.sh                 # easy mode
#    bash seren-agent-setup.sh --gen-token     # generate a bearer token
#    bash seren-agent-setup.sh --service       # + systemd autostart (sudo)
#    bash seren-agent-setup.sh --wheel ./seren_agent-1.0.0-py3-none-any.whl
#    bash seren-agent-setup.sh --ref v1.0.0    # pin to a release tag
#
#  FLAGS
#    --port N         Port to listen on            (default 7777)
#    --host HOST      Bind address                 (default 0.0.0.0)
#    --token TOKEN    Set a bearer token
#    --gen-token      Generate a random bearer token for you
#    --wheel PATH     Install from a local .whl instead of GitHub
#    --ref TAG        Pin to a GitHub release tag   (default: latest)
#    --repo SLUG      GitHub repo                   (default ChadRoesler/SerenAgent)
#    --service        Install + enable a systemd unit (needs sudo)
#    --venv PATH      Override venv location        (default ~/seren-venvs/agent)
#    -h, --help       This help
# ══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── pretty output ──────────────────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; NC='\033[0m'
step() { echo -e "\n${B}==>${NC} $1"; }
ok()   { echo -e "${G}  ✓${NC} $1"; }
warn() { echo -e "${Y}  !${NC} $1"; }
die()  { echo -e "${R}ERROR:${NC} $1" >&2; exit 1; }

# ── defaults ───────────────────────────────────────────────────────────────
PORT=7777
HOST="0.0.0.0"
TOKEN=""
GEN_TOKEN=false
WHEEL=""
REF=""
REPO="ChadRoesler/SerenAgent"
INSTALL_SERVICE=false
VENV_DIR="$HOME/seren-venvs/agent"

# ── flag parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)      PORT="$2"; shift 2 ;;
    --host)      HOST="$2"; shift 2 ;;
    --token)     TOKEN="$2"; shift 2 ;;
    --gen-token) GEN_TOKEN=true; shift ;;
    --wheel)     WHEEL="$2"; shift 2 ;;
    --ref)       REF="$2"; shift 2 ;;
    --repo)      REPO="$2"; shift 2 ;;
    --service)   INSTALL_SERVICE=true; shift ;;
    --venv)      VENV_DIR="$2"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)           die "unknown flag: $1  (try --help)" ;;
  esac
done

echo -e "${G}══════════════════════════════════════════${NC}"
echo -e "${G}  seren-agent setup (Linux)${NC}"
echo -e "${G}══════════════════════════════════════════${NC}"

# ── 1. find a usable Python ────────────────────────────────────────────────
step "Finding a usable Python (3.10-3.12)"
PYBIN=""
for cand in python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "")"
    case "$ver" in
      3.10|3.11|3.12) PYBIN="$cand"; break ;;
    esac
  fi
done
[[ -n "$PYBIN" ]] || die "No Python 3.10-3.12 found.
  Install one, e.g.:
    Debian/Ubuntu:  sudo apt install python3.12 python3.12-venv
    Fedora:         sudo dnf install python3.12
    Arch:           sudo pacman -S python"
PYVER="$("$PYBIN" -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])')"
ok "Using $PYBIN (Python $PYVER)"

# ── 2. resolve the wheel to install ────────────────────────────────────────
WHEEL_SRC=""
CLEANUP_WHEEL=false
if [[ -n "$WHEEL" ]]; then
  [[ -f "$WHEEL" ]] || die "wheel not found: $WHEEL"
  WHEEL_SRC="$WHEEL"
  ok "Installing from local wheel: $(basename "$WHEEL")"
else
  step "Resolving the latest seren-agent release from GitHub ($REPO)"
  command -v curl >/dev/null 2>&1 || die "curl is required (sudo apt install curl)"
  api="https://api.github.com/repos/${REPO}/releases/${REF:+tags/$REF}"
  [[ -z "$REF" ]] && api="https://api.github.com/repos/${REPO}/releases/latest"
  json="$(curl -fsSL "$api" 2>/dev/null)" || die "GitHub API request failed ($api). Check repo/tag and your network."
  read -r TAG WHL_URL < <("$PYBIN" - "$json" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
tag = data.get("tag_name", "?")
whl = next((a["browser_download_url"] for a in data.get("assets", [])
            if a.get("name", "").endswith(".whl")), "")
print(tag, whl)
PY
)
  [[ -n "$WHL_URL" && "$WHL_URL" != "None" ]] || die "No .whl asset in release '$TAG'. Pass --wheel to install a local file."
  ok "Release $TAG  ($(basename "$WHL_URL"))"
  WHEEL_SRC="$(mktemp --suffix=.whl)"
  CLEANUP_WHEEL=true
  trap '[[ "$CLEANUP_WHEEL" == true ]] && rm -f "$WHEEL_SRC"' EXIT
  curl -fsSL "$WHL_URL" -o "$WHEEL_SRC" || die "download failed"
  ok "Downloaded"
fi

# ── 3. venv + install ──────────────────────────────────────────────────────
step "Creating venv at $VENV_DIR"
if [[ -x "$VENV_DIR/bin/python" ]]; then
  warn "venv already exists - reusing it (will upgrade the package)"
else
  "$PYBIN" -m venv "$VENV_DIR" || die "venv creation failed (need python3-venv?)"
  ok "venv created"
fi
VPY="$VENV_DIR/bin/python"

step "Installing seren-agent"
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q --upgrade "$WHEEL_SRC" || die "pip install failed - see output above"
ok "Installed"

# ── 4. sanity check ────────────────────────────────────────────────────────
step "Sanity-checking the install"
CHECK="$("$VPY" -c 'import seren_agent; print("OK: v" + seren_agent.__version__)' 2>&1)"
case "$CHECK" in
  OK:*) ok "Package imports cleanly ($CHECK)" ;;
  *)    die "Install looks broken: $CHECK" ;;
esac

# ── 5. launcher ────────────────────────────────────────────────────────────
LAUNCHER="$HOME/run-seren-agent.sh"
cat > "$LAUNCHER" <<SH
#!/usr/bin/env bash
# Start seren-agent. Run this directly or have systemd call it.
export AGENT_HOST="${HOST}"
export AGENT_PORT="${PORT}"
$( [[ -n "$TOKEN" ]] && echo "export SEREN_AGENT_TOKEN=\"${TOKEN}\"" )
exec "$VPY" -m seren_agent.app
SH
chmod +x "$LAUNCHER"
ok "Launcher: $LAUNCHER"

# ── 6. optional systemd service ────────────────────────────────────────────
if $INSTALL_SERVICE; then
  step "Installing systemd service (needs sudo)"
  UNIT=/etc/systemd/system/seren-agent.service
  $GEN_TOKEN && TOKEN="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(32))')"
  sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=seren-agent - per-Jetson management plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Environment=AGENT_HOST=${HOST}
Environment=AGENT_PORT=${PORT}
$( [[ -n "$TOKEN" ]] && echo "Environment=SEREN_AGENT_TOKEN=${TOKEN}" )
ExecStart=${VPY} -m seren_agent.app
Restart=on-failure
RestartSec=5
KillMode=process

[Install]
WantedBy=multi-user.target
UNITEOF
  sudo systemctl daemon-reload
  sudo systemctl enable --now seren-agent
  ok "Service installed and started"
  step "Waiting for it to come up"
  for i in $(seq 1 30); do
    sleep 0.5
    if curl -fsS "http://127.0.0.1:${PORT}/api/v1/system/ping" >/dev/null 2>&1; then
      ok "seren-agent is responding"; break
    fi
    [[ $i -eq 30 ]] && warn "Didn't respond in 15s - check: journalctl -u seren-agent -f"
  done
fi

# ── done ───────────────────────────────────────────────────────────────────
echo
echo -e "${G}══════════════════════════════════════════${NC}"
echo -e "${G}  seren-agent is set up ✓${NC}"
echo -e "${G}══════════════════════════════════════════${NC}"
if ! $INSTALL_SERVICE; then
  echo -e "  Start it:   ${B}$LAUNCHER${NC}"
fi
echo -e "  Ping:       ${B}http://${HOST}:${PORT}/api/v1/system/ping${NC}"
echo -e "  Docs:       ${B}http://${HOST}:${PORT}/docs${NC}"
[[ -n "$TOKEN" ]] && echo -e "  Token:      ${Y}${TOKEN}${NC}"
echo
echo -e "${G}Rip it and win. 🌭🔧${NC}"