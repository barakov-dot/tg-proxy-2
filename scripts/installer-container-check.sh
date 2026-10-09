#!/usr/bin/env bash
# Runs INSIDE a clean ubuntu:24.04 / debian:13 container (see Makefile and CI job
# installer-containers): install.sh on a server without the WEB proxy must exit 1, say so,
# and change nothing.
set -euo pipefail

apt-get update -qq
apt-get install -y -qq curl ca-certificates >/dev/null

snap() {
  find /etc /opt /usr/local /var/lib /var/backups -xdev -printf '%p %s %T@\n' 2>/dev/null |
    sort | sha256sum
}

before="$(snap)"
set +e
bash /src/install.sh --panel-domain panel.example.com \
  --bot-token 123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI --admin-id 1 --yes --no-import \
  >/tmp/out.txt 2>&1
rc=$?
set -e
cat /tmp/out.txt
[ "$rc" = 1 ]
grep -q "WEB proxy не найден" /tmp/out.txt
[ "$before" = "$(snap)" ]
echo "OK: exit 1, nothing changed"
