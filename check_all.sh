#!/usr/bin/env bash
# Runs every self-check, stops at the first failure. Needs docker and internet; takes a few minutes.
set -uo pipefail
cd "$(dirname "$0")"

for c in "sidecar/proxy.py" "telegram_api.py" "sandbox.py" "check_base.py" "dispatch.py" "agent.py" "check_voice_adapter.py" "bot.py --check"; do
  if out=$(python3 $c 2>&1); then
    echo "ok      $c"
  else
    echo "FAILED  $c"; echo "$out" | tail -30; exit 1
  fi
done

left=$(docker ps -aq --filter name=tb- | wc -l)
[ "$left" = 0 ] || { echo "FAILED  $left containers left behind (tb-*)"; exit 1; }
echo "all checks passed"
