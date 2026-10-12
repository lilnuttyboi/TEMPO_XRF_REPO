#!/usr/bin/env bash
# Start the TEMPO XRF frontend using only software already installed locally.
# This launcher never runs git, pip, dnf, npm, curl, or another network command.

set -Eeuo pipefail

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"

# A checked-in copy uses its own directory. A copy placed in /home/mnode falls
# back to the shared cluster repository used in production.
if [[ -n "${TEMPO_XRF_REPO_DIR:-}" ]]; then
    REPO_DIR="$TEMPO_XRF_REPO_DIR"
elif [[ -d "$SCRIPT_DIR/python_code/roboai_xrf_frontend" ]]; then
    REPO_DIR="$SCRIPT_DIR"
elif [[ -d /srv/cluster/TEMPO_XRF_REPO/python_code/roboai_xrf_frontend ]]; then
    REPO_DIR=/srv/cluster/TEMPO_XRF_REPO
else
    fail "TEMPO XRF repository not found. Set TEMPO_XRF_REPO_DIR to its path."
fi

FRONTEND_DIR="$REPO_DIR/python_code/roboai_xrf_frontend"
[[ -f "$FRONTEND_DIR/main.py" ]] || fail "Frontend entry point not found: $FRONTEND_DIR/main.py"

# The first argument can override the port. --check validates without starting.
CHECK_ONLY=0
case "${1:-}" in
    --check)
        CHECK_ONLY=1
        PORT="${TEMPO_XRF_PORT:-8502}"
        ;;
    --help|-h)
        echo "Usage: $0 [PORT|--check]"
        exit 0
        ;;
    *)
        PORT="${1:-${TEMPO_XRF_PORT:-8502}}"
        ;;
esac
ADDRESS="${TEMPO_XRF_ADDRESS:-0.0.0.0}"
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "Port must be a number between 1 and 65535."
(( PORT >= 1 && PORT <= 65535 )) || fail "Port must be between 1 and 65535."

if [[ -n "${TEMPO_XRF_GEANT4_SETUP:-}" ]]; then
    GEANT4_SETUP="$TEMPO_XRF_GEANT4_SETUP"
elif [[ -r /srv/cluster/software/geant4-11.4.2/bin/geant4.sh ]]; then
    GEANT4_SETUP=/srv/cluster/software/geant4-11.4.2/bin/geant4.sh
elif [[ -r "$HOME/geant4/geant4-install/bin/geant4.sh" ]]; then
    GEANT4_SETUP="$HOME/geant4/geant4-install/bin/geant4.sh"
else
    fail "Geant4 setup not found. Set TEMPO_XRF_GEANT4_SETUP to geant4.sh."
fi

if [[ -n "${TEMPO_XRF_VENV:-}" ]]; then
    VENV_DIR="$TEMPO_XRF_VENV"
elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    VENV_DIR="$REPO_DIR/.venv"
elif [[ -x "$HOME/TEMPO_XRF_REPO/.venv/bin/python" ]]; then
    VENV_DIR="$HOME/TEMPO_XRF_REPO/.venv"
else
    fail "Python environment not found. Set TEMPO_XRF_VENV to the existing virtualenv."
fi

echo "Starting TEMPO XRF"
echo "  Repository: $REPO_DIR"
echo "  Geant4:     $GEANT4_SETUP"
echo "  Python:     $VENV_DIR/bin/python"
echo "  Listen:     http://$ADDRESS:$PORT"
echo "Press Ctrl+C to stop."

# shellcheck disable=SC1090
source "$GEANT4_SETUP"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

export PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
# Do not attempt to send Streamlit usage telemetry from the offline management node.
export STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

cd "$FRONTEND_DIR"

if ! python -c 'import builder, numpy, pandas, plotly, roboaixrf, spekpy, streamlit, uproot, xraylib'; then
    fail "The existing virtualenv is missing a required package or project import. No download was attempted."
fi

if (( CHECK_ONLY )); then
    echo "Offline preflight passed; no download or server start was attempted."
    exit 0
fi

exec python -m streamlit run main.py \
    --server.headless=true \
    --server.address="$ADDRESS" \
    --server.port="$PORT" \
    --browser.gatherUsageStats=false
