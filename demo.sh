#!/usr/bin/env bash
# demo.sh - the whole talk, one step per screen. Press Enter to move on.
#
#   ./demo.sh            # live, local Qwen via Ollama (default)
#   ./demo.sh mock       # scripted replay - rehearsal or fallback
#   ./demo.sh anthropic  # live Claude (needs ANTHROPIC_API_KEY)
#
# Step 2 always runs on Claude for contrast. Without ANTHROPIC_API_KEY it is skipped.

cd "$(dirname "$0")" || exit 1
PROV="${1:-ollama}"
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python"
BOLD=$(tput bold 2>/dev/null); DIM=$(tput dim 2>/dev/null); RESET=$(tput sgr0 2>/dev/null)

step () {
  clear
  echo "${DIM}Build it. Break it. Secure it.   ·   provider: $PROV${RESET}"
  echo
  echo "${BOLD}STEP $1  ·  $2${RESET}"
  shift 2
  if [[ "$*" == *anthropic* && -z "$ANTHROPIC_API_KEY" ]]; then
    echo; echo "Skipped: run  export ANTHROPIC_API_KEY=...  before the demo to show Claude."; echo
  else
    # A --provider in the step's own arguments overrides $PROV (the last one wins).
    "$PY" src/agent.py --provider "$PROV" "$@"
  fi
  read -r -p "${DIM}[Enter] next step${RESET}"
}

step 1 "MaintainerBot triages a new issue, NO defences"                --task triage
step 2 "Same attack, smarter model: Claude Opus 5.5, NO defences"     --task triage --provider anthropic
step 3 "Layer 1: allowlist (the bot can only read what it needs)"     --task triage --layers 1
step 4 "Layer 2: human approval (YOU decide, type n)"                 --task triage --layers 2
step 5 "Layer 3: dual LLM (the bot never sees the raw issue)"         --task triage --layers 3
step 6 "Layer 4: output guard (last check before going public)"       --task triage --layers 4
step 7 "Second scenario: the bot overreaches, NO defences"            --task overreach
step 8 "Overreach, with layer 1: allowlist"                           --task overreach --layers 1
step 9 "Everything on"                                                --task triage --layers 1,2,3,4

clear
echo
echo "${BOLD}The model is never the control. The code around it is.${RESET}"
echo
