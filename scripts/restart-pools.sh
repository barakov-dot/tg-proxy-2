#!/usr/bin/env bash
# Restart all tgpanel-mtproxy@* pools one after another (called by
# tgpanel-mtproxy-refresh.service when proxy-multi.conf changes).
set -euo pipefail

mapfile -t units < <(systemctl list-units --plain --no-legend --state=active \
  'tgpanel-mtproxy@*.service' | awk '{print $1}')
for unit in "${units[@]}"; do
  [ -n "$unit" ] || continue
  systemctl restart "$unit"
  sleep 2
done
