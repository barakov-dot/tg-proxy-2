#!/usr/bin/env bash
# Restart all tgpanel-mtproxy@* pools one after another (called by
# tgpanel-mtproxy-refresh.service when proxy-multi.conf changes).
set -euo pipefail

# Same lock as the apply pipeline (tgpanel.apply): never restart pools mid-apply.
LOCK=/var/lib/tgpanel/apply.lock
exec 9>"$LOCK"
flock -w 120 9 || {
  echo "не удалось получить блокировку применения $LOCK за 120 с" >&2
  exit 1
}

mapfile -t units < <(systemctl list-units --plain --no-legend --state=active \
  'tgpanel-mtproxy@*.service' | awk '{print $1}')
for unit in "${units[@]}"; do
  [ -n "$unit" ] || continue
  systemctl restart "$unit"
  sleep 2
done
