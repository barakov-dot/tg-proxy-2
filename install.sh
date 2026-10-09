#!/usr/bin/env bash
# tgpanel installer (PLAN 8.1 / 8.2). Idempotent: running it again updates the installation and
# never regenerates the panel path, login, password or application secrets.
#
#   curl -fsSL https://raw.githubusercontent.com/barakov-dot/tg-proxy-2/main/install.sh | bash -s -- \
#     --panel-domain panel.example.com --bot-token-file /root/bot.token --admin-id 123456
#
# Options: --panel-domain D  --admin-id N  [--bot-token T | --bot-token-file F]
#          [--panel-login L] [--panel-password P] [--ref REF] [--yes] [--import | --no-import]
#          [--ignore-dns] [--check-only]
# Secrets may also come from the environment (not visible in `ps`):
#   TGPANEL_INSTALL_BOT_TOKEN, TGPANEL_INSTALL_PANEL_PASSWORD
# Without a terminal (/dev/tty) the installer needs --yes and --import/--no-import (decided
# BEFORE anything is changed). --yes never skips the DNS check: only --ignore-dns does.
# Without --ref: newest v*.*.* release tag, else the moving `main` branch (a warning is printed).
#
# Test hooks (never needed in production):
#   TGPANEL_ROOT_PREFIX=<dir>  every absolute path (/etc, /usr, /opt, ...) is looked up under
#                              <dir>; root/OS/arch/systemd checks are replaced by marker files
#                              (<dir>/run/active/<unit>, <dir>/run/healthz-ok) and network
#                              checks are skipped unless TGPANEL_FORCE_NET_CHECKS=1.
#   TGPANEL_SOURCE_ONLY=1      define the functions only (no main), for unit tests.
#   --check-only               run only the read-only checks (steps 1-3) and exit.
#
# Order: step 2 (is the WEB proxy installed?) runs FIRST and is read-only: when it fails the
# script prints every failed check plus the upstream README link and exits 1 changing nothing.
#
# Written for bash 3.2+ (no mapfile, no associative arrays) so it can be tested on macOS.
set -Eeuo pipefail

REPO_URL="${TGPANEL_REPO_URL:-https://github.com/barakov-dot/tg-proxy-2.git}"
UPSTREAM_README="https://github.com/telegramdesktop/tproxy-server#readme"
P="${TGPANEL_ROOT_PREFIX:-}"

INSTALL_DIR="$P/opt/tgpanel"
VENV_PY="$INSTALL_DIR/.venv/bin/python"
ETC_DIR="$P/etc/tgpanel"
ENV_FILE="$ETC_DIR/tgpanel.env"
CRED_FILE="$ETC_DIR/.first-run-credentials"
STATE_DIR="$P/var/lib/tgpanel"
BACKUP_DIR="$P/var/backups/tgpanel"
SYSTEMD_DIR="$P/etc/systemd/system"
CLI_LINK="$P/usr/local/bin/tgpanel"
LOCK_FILE="$P/run/tgpanel-install.lock"
PACKAGES="python3 python3-venv git sqlite3 nftables ca-certificates tzdata"

PANEL_DOMAIN=""
BOT_TOKEN="${TGPANEL_INSTALL_BOT_TOKEN:-}"
ADMIN_ID=""
PANEL_LOGIN=""
PANEL_PASSWORD="${TGPANEL_INSTALL_PANEL_PASSWORD:-}"
REF=""
REF_GIVEN=0
ASSUME_YES=0
IMPORT_MODE=""
IGNORE_DNS=0
CHECK_ONLY=0
UPDATE=0
HAVE_TTY=0
PANEL_PATH=""
SECRET_KEY=""
BACKUP_PATH=""
STORED_DOMAIN=""
STORED_TOKEN=""
PROBE_ACTIVE=0
POLICY_RC_CREATED=""
TMP_FILES=()

log() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
die() {
  printf 'ОШИБКА: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local f
  if [ "${#TMP_FILES[@]}" -gt 0 ]; then
    for f in "${TMP_FILES[@]}"; do rm -f "$f"; done
  fi
  if [ "$PROBE_ACTIVE" = 1 ]; then
    nft delete table inet tgpanel_install_probe >/dev/null 2>&1 || true
  fi
  if [ -n "$POLICY_RC_CREATED" ]; then rm -f "$POLICY_RC_CREATED"; fi
}
trap cleanup EXIT
trap 'die "сбой на строке $LINENO"' ERR
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

usage() {
  sed -n '2,19p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null || true
}

# ------------------------------------------------------------------------ helpers

# Read one line from the terminal (works under `curl | bash`). Args: variable prompt [secret].
ask() {
  local __var="$1" __prompt="$2" __secret="${3:-}" __reply=""
  [ "$HAVE_TTY" = 1 ] || die "нужно значение «$__prompt», а терминала нет: передайте его аргументом"
  if [ -n "$__secret" ]; then
    read -r -s -p "$__prompt" __reply </dev/tty || die "не удалось прочитать ответ с терминала"
    printf '\n'
  else
    read -r -p "$__prompt" __reply </dev/tty || die "не удалось прочитать ответ с терминала"
  fi
  printf -v "$__var" '%s' "$__reply"
}

# Yes/no, default NO. --yes answers yes (use confirm_strict for questions --yes must not skip).
confirm() {
  if [ "$ASSUME_YES" = 1 ]; then return 0; fi
  confirm_strict "$@"
}

confirm_strict() {
  local reply=""
  [ "$HAVE_TTY" = 1 ] || return 1
  read -r -p "$1 [y/N] " reply </dev/tty || return 1
  case "$reply" in y | Y | yes | YES | д | Д | да | Да) return 0 ;; *) return 1 ;; esac
}

# URL-safe random string of length $1 whose FIRST character is alphanumeric (a value starting
# with '-' would otherwise be taken for an option by argparse).
random_token() {
  local n="$1" first rest=""
  first="$(head -c 256 /dev/urandom | LC_ALL=C tr -dc 'A-Za-z0-9' | head -c 1)"
  while [ "${#rest}" -lt "$n" ]; do
    rest="$rest$(head -c 48 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')"
  done
  printf '%s' "$first${rest:0:$((n - 1))}"
}

env_get() { # key -> value from the env file (never sourced)
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^$1=//p" "$ENV_FILE" | head -n 1
}

env_set() { # key value (atomic, keeps 0600)
  local tmp
  tmp="$(umask 077 && mktemp "$ETC_DIR/.env.XXXXXX")"
  TMP_FILES+=("$tmp")
  grep -v "^$1=" "$ENV_FILE" >"$tmp" || true
  printf '%s=%s\n' "$1" "$2" >>"$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

net_checks_enabled() { [ -z "$P" ] || [ "${TGPANEL_FORCE_NET_CHECKS:-}" = 1 ]; }

unit_active() {
  if [ -n "$P" ]; then [ -e "$P/run/active/$1" ]; else systemctl is-active --quiet "$1"; fi
}

any_pool_active() {
  if [ -n "$P" ]; then
    local f
    for f in "$P"/run/active/tgpanel-mtproxy@*; do [ -e "$f" ] && return 0; done
    return 1
  fi
  systemctl list-units --plain --no-legend --state=active 'tgpanel-mtproxy@*' 2>/dev/null |
    grep -q .
}

# The old mtproxy was switched off on purpose (tgpanel legacy-mtproxy off): masked, or the
# tgpanel-off drop-in exists.
legacy_deliberately_off() {
  local unit="$P/etc/systemd/system/mtproxy.service"
  [ -e "$unit.d/tgpanel-off.conf" ] && return 0
  [ -L "$unit" ] && [ "$(readlink "$unit")" = /dev/null ] && return 0
  if [ -z "$P" ] && [ "$(systemctl is-enabled mtproxy 2>/dev/null || true)" = masked ]; then
    return 0
  fi
  return 1
}

relay_healthy() {
  if [ -n "$P" ]; then [ -e "$P/run/healthz-ok" ]; else
    curl -fsS --max-time 5 http://127.0.0.1:8081/healthz >/dev/null 2>&1
  fi
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# getMe through a curl config on stdin: the token never appears in the process arguments.
telegram_get_me() {
  printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$1" |
    curl -fsS --max-time 10 -K - 2>/dev/null
}

run_ops() { (cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR" "$VENV_PY" -m tgpanel.ops_cli "$@"); }

# --------------------------------------------------------------------- argument parsing

parse_args() {
  local opt
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --panel-domain) PANEL_DOMAIN="${2:?--panel-domain требует значение}"; shift 2 ;;
      --bot-token) BOT_TOKEN="${2:?--bot-token требует значение}"; shift 2 ;;
      --bot-token-file)
        [ -r "${2:?--bot-token-file требует путь}" ] || die "файл с токеном недоступен"
        BOT_TOKEN="$(head -n 1 "$2" | tr -d '[:space:]')"
        shift 2
        ;;
      --admin-id) ADMIN_ID="${2:?--admin-id требует значение}"; shift 2 ;;
      --panel-login) PANEL_LOGIN="${2:?--panel-login требует значение}"; shift 2 ;;
      --panel-password) PANEL_PASSWORD="${2:?--panel-password требует значение}"; shift 2 ;;
      --ref) REF="${2:?--ref требует значение}"; REF_GIVEN=1; shift 2 ;;
      --yes | -y) ASSUME_YES=1; shift ;;
      --import) IMPORT_MODE=yes; shift ;;
      --no-import) IMPORT_MODE=no; shift ;;
      --ignore-dns) IGNORE_DNS=1; shift ;;
      --check-only) CHECK_ONLY=1; shift ;;
      -h | --help) usage; exit 0 ;;
      --*) opt="${1%%=*}"; die "неизвестный аргумент: $opt" ;;
      *) die "неизвестный позиционный аргумент" ;;
    esac
  done
  if [ "$REF_GIVEN" = 1 ]; then
    printf '%s' "$REF" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,99}$' || die "некорректный --ref"
    case "$REF" in *..*) die "некорректный --ref" ;; esac
  fi
}

detect_tty() {
  if (: </dev/tty) 2>/dev/null; then HAVE_TTY=1; else HAVE_TTY=0; fi
}

validate_inputs() {
  printf '%s' "$PANEL_DOMAIN" | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' ||
    die "некорректный домен панели (строчные буквы, цифры, дефис, точки)"
  printf '%s' "$BOT_TOKEN" | grep -Eq '^[0-9]{5,}:[A-Za-z0-9_-]{30,}$' ||
    die "токен бота имеет неверный формат"
  printf '%s' "$ADMIN_ID" | grep -Eq '^[0-9]{1,15}$' || die "admin-id должен быть числом"
  if [ -n "$PANEL_LOGIN" ]; then
    printf '%s' "$PANEL_LOGIN" | grep -Eq '^[A-Za-z0-9_.-]{3,32}$' ||
      die "логин: 3-32 символа [A-Za-z0-9_.-]"
  fi
  if [ -n "$PANEL_PASSWORD" ] && [ "${#PANEL_PASSWORD}" -lt 12 ]; then
    die "пароль панели должен быть не короче 12 символов"
  fi
}

# --------------------------------------------------------- step 1: system requirements

check_system() {
  step "Шаг 1. Система"
  command -v curl >/dev/null 2>&1 || die "не найден curl (apt-get install curl)"
  if [ -n "$P" ]; then
    log "тестовый режим (корень $P): проверки root/ОС/архитектуры пропущены"
    return 0
  fi
  [ "$(id -u)" = 0 ] || die "запустите от root"
  [ "$(uname -m)" = x86_64 ] || die "поддерживается только x86_64 (у вас $(uname -m))"
  local id="" version=""
  if [ -r /etc/os-release ]; then
    id="$(sed -n 's/^ID=//p' /etc/os-release | tr -d '"')"
    version="$(sed -n 's/^VERSION_ID=//p' /etc/os-release | tr -d '"')"
  fi
  case "$id:$version" in
    ubuntu:24.04 | debian:13) log "ОС: $id $version" ;;
    *) die "поддерживаются Ubuntu 24.04 и Debian 13 (у вас: ${id:-?} ${version:-?})" ;;
  esac
}

# ----------------------------------------- step 2: is the WEB proxy installed? (read-only)

check_proxy_installed() {
  step "Шаг 2. Проверка, что WEB proxy установлен (ничего не меняется)"
  local failed=() f u
  for f in /usr/local/bin/tproxy-server /etc/tproxy-server/config.json \
    /etc/tproxy-server/profiles.json /opt/MTProxy/objs/bin/mtproto-proxy \
    /usr/local/bin/caddy /etc/caddy/Caddyfile; do
    [ -e "$P$f" ] || failed+=("нет файла $f")
  done
  for u in tproxy-server caddy; do
    unit_active "$u" || failed+=("служба $u не активна")
  done
  if ! unit_active mtproxy; then
    if [ "$UPDATE" = 1 ] && { any_pool_active || legacy_deliberately_off; }; then
      log "старый mtproxy не активен: это нормально (пулы tgpanel работают или он выключен командой legacy-mtproxy off)"
    else
      failed+=("служба mtproxy не активна")
    fi
  fi
  relay_healthy || failed+=("http://127.0.0.1:8081/healthz не отвечает")
  if [ "${#failed[@]}" -gt 0 ]; then
    printf 'WEB proxy не найден или неисправен. Не прошли проверки:\n' >&2
    local item
    for item in "${failed[@]}"; do printf '  - %s\n' "$item" >&2; done
    printf 'Сначала установите tproxy-server по инструкции: %s\n' "$UPSTREAM_README" >&2
    printf 'Установщик остановлен; на сервере ничего не изменено.\n' >&2
    exit 1
  fi
  log "WEB proxy на месте: файлы, службы и /healthz в порядке"
}

# ---------------------------------------------------------------- step 3: preconditions

proxy_hostname() {
  local f
  for f in "$P"/etc/systemd/system/caddy.service.d/*.conf; do
    [ -f "$f" ] || continue
    sed -n 's/.*TPROXY_HOSTNAME=\([^ "]*\).*/\1/p' "$f"
  done | head -n 1
}

check_domain_differs() {
  local host
  host="$(proxy_hostname)"
  if [ -n "$host" ] && [ "$host" = "$PANEL_DOMAIN" ]; then
    die "домен панели совпадает с именем хоста прокси ($host): нужен отдельный поддомен"
  fi
}

verified_list() {
  local here=""
  here="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
  if [ -n "${TGPANEL_VERIFIED_LIST:-}" ]; then printf '%s' "$TGPANEL_VERIFIED_LIST"
  elif [ -f "$INSTALL_DIR/deploy/verified-relay.sha256" ]; then
    printf '%s' "$INSTALL_DIR/deploy/verified-relay.sha256"
  elif [ -n "$here" ] && [ -f "$here/deploy/verified-relay.sha256" ]; then
    printf '%s' "$here/deploy/verified-relay.sha256"
  fi
}

check_relay_version() {
  local list sha
  sha="$(sha256_of "$P/usr/local/bin/tproxy-server")"
  list="$(verified_list)"
  if [ -z "$list" ]; then
    warn "список проверенных версий relay недоступен; sha256 relay: $sha"
  elif ! grep -v '^[[:space:]]*#' "$list" | grep -qi "^$sha"; then
    if grep -v '^[[:space:]]*#' "$list" | grep -Eq '^[0-9a-fA-F]{64}'; then
      warn "версия relay не из списка проверенных (sha256 $sha); панель может работать неверно"
    else
      warn "список проверенных версий relay пуст; sha256 relay: $sha"
    fi
  else
    log "версия relay входит в список проверенных"
  fi
}

public_ipv4() {
  local url ip
  for url in https://api.ipify.org https://checkip.amazonaws.com https://ipv4.icanhazip.com; do
    ip="$(curl -4 -fsS --max-time 8 "$url" 2>/dev/null | tr -d '[:space:]' || true)"
    if printf '%s' "$ip" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
      printf '%s' "$ip"
      return 0
    fi
  done
  return 1
}

check_dns() {
  local my_ip a_records aaaa_records problems="" rec local6
  my_ip="$(public_ipv4 || true)"
  a_records="$(getent ahostsv4 "$PANEL_DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  # v4-mapped ::ffff: lines are synthesised by the resolver, not real AAAA records
  aaaa_records="$(getent ahostsv6 "$PANEL_DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u |
    grep -vi '^::ffff:' || true)"
  if [ -z "$my_ip" ]; then
    problems="не удалось определить публичный IPv4 сервера; "
  elif [ -z "$a_records" ]; then
    problems="${problems}у $PANEL_DOMAIN нет A-записи; "
  elif ! printf '%s\n' "$a_records" | grep -qx "$my_ip"; then
    problems="${problems}A-запись $PANEL_DOMAIN ($(printf '%s' "$a_records" | tr '\n' ' ')) не указывает на этот сервер ($my_ip); "
  fi
  if [ -n "$aaaa_records" ]; then
    local6="$(ip -6 -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 || true)"
    for rec in $aaaa_records; do
      printf '%s\n' "$local6" | grep -qix "$rec" ||
        problems="${problems}AAAA-запись $rec не ведёт на этот сервер: выпуск сертификата сломается, удалите её; "
    done
  fi
  if [ -z "$problems" ]; then
    log "DNS: $PANEL_DOMAIN → $my_ip"
    return 0
  fi
  warn "DNS домена панели: $problems"
  warn "Caddy не получит сертификат, пока DNS не исправлен (A на IP сервера, AAAA нет или тоже на сервер)."
  if [ "$IGNORE_DNS" = 1 ]; then return 0; fi
  # --yes does NOT skip this question
  confirm_strict "Продолжить несмотря на DNS? (по умолчанию — прервать)" ||
    die "прервано: исправьте DNS и запустите установщик снова (или добавьте --ignore-dns)"
}

check_ports_free() {
  local busy
  busy="$(ss -Hltn 2>/dev/null | awk '{n=split($4,a,":"); p=a[n]+0;
    if ((p>=2400&&p<=2463)||(p>=8900&&p<=8963)||p==8090) print p}' | sort -un | tr '\n' ' ' || true)"
  if [ -n "$busy" ]; then
    die "заняты порты, нужные панели: $busy (диапазоны 2400-2463, 8900-8963 и 8090 должны быть свободны)"
  fi
  log "порты 2400-2463, 8900-8963, 8090 свободны"
}

# Ports 80 and 443 must be listened on by caddy (PLAN 3.8): ACME HTTP-01/TLS-ALPN needs them.
check_caddy_ports() {
  local port line
  for port in 80 443; do
    line="$(ss -Hltnp 2>/dev/null | awk -v p=":$port" '$4 ~ (p "$") {print}' | head -n 1 || true)"
    if [ -z "$line" ]; then
      die "порт $port никто не слушает: Caddy должен слушать 80 и 443 (иначе сертификат не выпустить)"
    fi
    printf '%s' "$line" | grep -q 'caddy' ||
      die "порт $port занят не Caddy: $(printf '%s' "$line" | awk '{print $NF}')"
  done
  log "порты 80 и 443 слушает Caddy"
}

probe_nft_counters() {
  local tmp
  tmp="$(mktemp)"
  TMP_FILES+=("$tmp")
  cat >"$tmp" <<'EOF'
table inet tgpanel_install_probe
delete table inet tgpanel_install_probe
table inet tgpanel_install_probe {
	set s {
		type ipv4_addr
		counter
		elements = { 127.64.255.254 }
	}
}
EOF
  PROBE_ACTIVE=1 # the EXIT trap deletes the table even if we are interrupted
  if nft -f "$tmp" >/dev/null 2>&1 && nft list set inet tgpanel_install_probe s >/dev/null 2>&1; then
    nft delete table inet tgpanel_install_probe >/dev/null 2>&1 || true
    PROBE_ACTIVE=0
    log "nftables: счётчики на элементах наборов работают"
    return 0
  fi
  nft delete table inet tgpanel_install_probe >/dev/null 2>&1 || true
  PROBE_ACTIVE=0
  die "nftables не поддерживает счётчики на элементах наборов (нужны nftables >= 0.9.5 и ядро >= 5.7)"
}

check_bot_token() {
  [ -n "$BOT_TOKEN" ] || return 0
  local reply
  reply="$(telegram_get_me "$BOT_TOKEN" || true)"
  if [ -z "$reply" ]; then
    warn "не удалось проверить токен бота (нет ответа Telegram)"
  elif ! printf '%s' "$reply" | grep -q '"ok":true'; then
    die "Telegram не принял токен бота"
  fi
}

preflight_checks() {
  step "Шаг 3. Проверки перед установкой"
  if [ -n "$PANEL_DOMAIN" ]; then check_domain_differs; fi
  check_relay_version
  if ! net_checks_enabled; then
    log "тестовый режим: DNS, порты и сеть пропущены"
    return 0
  fi
  if [ -n "$PANEL_DOMAIN" ]; then check_dns; fi
  check_caddy_ports
  if [ "$UPDATE" = 0 ]; then check_ports_free; fi
  check_bot_token
}

# ------------------------------------------------------------------- steps 4-11 (changes)

missing_packages() {
  local p
  for p in $PACKAGES; do
    dpkg -s "$p" >/dev/null 2>&1 || printf '%s ' "$p"
  done
}

install_packages() {
  step "Шаг 4. Пакеты"
  local missing pkgs=()
  missing="$(missing_packages)"
  if [ -n "$missing" ]; then
    read -r -a pkgs <<<"$missing"
    export DEBIAN_FRONTEND=noninteractive
    # a freshly installed nftables must not be started: its service would `flush ruleset`
    # and wipe the upstream tables
    if [ ! -e /usr/sbin/policy-rc.d ]; then
      printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
      chmod 0755 /usr/sbin/policy-rc.d
      POLICY_RC_CREATED=/usr/sbin/policy-rc.d
    fi
    apt-get update -qq
    apt-get install -y -qq --no-upgrade "${pkgs[@]}" >/dev/null
    log "установлено: $missing"
  else
    log "все нужные пакеты уже установлены: apt не вызывается"
  fi
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' ||
    die "нужен Python 3.12 или новее (Ubuntu 24.04 даёт 3.12, Debian 13 — 3.13); у вас $(python3 -V 2>&1)"
  probe_nft_counters
}

latest_release_tag() {
  git -C "$INSTALL_DIR" tag -l 'v*.*.*' --sort=-v:refname | head -n 1 || true
}

resolve_ref() {
  local candidate commit
  for candidate in "origin/$REF" "$REF"; do
    if commit="$(git -C "$INSTALL_DIR" rev-parse --verify --quiet "${candidate}^{commit}")"; then
      printf '%s' "$commit"
      return 0
    fi
  done
  return 1
}

fetch_code() {
  step "Код и зависимости"
  local commit tag
  if [ -d "$INSTALL_DIR/.git" ]; then
    git -C "$INSTALL_DIR" fetch --tags --force --prune origin
  else
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
  if [ "$REF_GIVEN" = 0 ]; then
    tag="$(latest_release_tag)"
    if [ -n "$tag" ]; then
      REF="$tag"
      log "версия: последний релиз $tag"
    else
      REF="main"
      warn "релизных тегов нет: устанавливается движущаяся ветка main. Для фиксации версии: --ref <тег или коммит>"
    fi
  fi
  commit="$(resolve_ref)" || die "ветка, тег или коммит «$REF» не найдены в репозитории"
  git -C "$INSTALL_DIR" checkout --detach --force "$commit" >/dev/null 2>&1
  log "код: $REF ($(printf '%s' "$commit" | cut -c1-10))"
  check_relay_version # the list shipped with this exact version decides the warning
  if [ ! -x "$VENV_PY" ]; then python3 -m venv "$INSTALL_DIR/.venv"; fi
  "$VENV_PY" -m pip install --require-hashes --only-binary=:all: --no-input \
    --disable-pip-version-check -r "$INSTALL_DIR/requirements.lock" >/dev/null
  log "зависимости установлены (с проверкой хешей, только готовые пакеты)"
}

make_dirs_and_backup() {
  step "Шаги 5-6. Каталоги и исходная резервная копия"
  install -d -m 0700 "$ETC_DIR" "$STATE_DIR" "$BACKUP_DIR"
  BACKUP_PATH="$(run_ops pre-install-backup)"
  log "копия pre-install: $BACKUP_PATH (не ротируется)"
}

write_env() {
  step "Шаг 7. Доступы и секреты"
  PANEL_PATH="$(random_token 32)"
  SECRET_KEY="$(random_token 48)"
  [ -n "$PANEL_LOGIN" ] || PANEL_LOGIN="admin-$(random_token 6)"
  if [ -z "$PANEL_PASSWORD" ]; then PANEL_PASSWORD="$(random_token 20)"; fi
  local tmp
  tmp="$(umask 077 && mktemp "$ETC_DIR/.env.XXXXXX")"
  TMP_FILES+=("$tmp")
  {
    printf 'TGPANEL_BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'TGPANEL_ADMIN_IDS=%s\n' "$ADMIN_ID"
    printf 'TGPANEL_PANEL_DOMAIN=%s\n' "$PANEL_DOMAIN"
    printf 'TGPANEL_PANEL_PATH=%s\n' "$PANEL_PATH"
    printf 'TGPANEL_SECRET_KEY=%s\n' "$SECRET_KEY"
    printf 'TGPANEL_DB=%s\n' "/var/lib/tgpanel/tgpanel.db"
    printf 'TGPANEL_LISTEN=127.0.0.1:8090\n'
  } >"$tmp"
  chmod 0600 "$tmp"
  mv "$tmp" "$ENV_FILE"
  # kept (0600) until the final summary, so a failed install can still tell the password
  (umask 077 && printf 'login=%s\npassword=%s\n' "$PANEL_LOGIN" "$PANEL_PASSWORD" >"$CRED_FILE")
  log "создан $ENV_FILE (0600)"
}

show_credentials_now() {
  log ""
  log "Доступы к панели (запишите сейчас; повторно они будут показаны в итоге установки):"
  log "  Логин:  $PANEL_LOGIN"
  log "  Пароль: $PANEL_PASSWORD"
}

install_units() {
  step "Шаг 8. Юниты, таблица nft, база данных"
  local f
  for f in tgpanel.service tgpanel-firewall.service tgpanel-mtproxy-refresh.path \
    tgpanel-mtproxy-refresh.service; do
    install -m 0644 "$INSTALL_DIR/deploy/$f" "$SYSTEMD_DIR/$f"
  done
  install -m 0755 "$INSTALL_DIR/deploy/tgpanel-cli" "$CLI_LINK"
  chmod 0755 "$INSTALL_DIR/scripts/restart-pools.sh"
  systemctl daemon-reload
  # tgpanel.nft appears with the first apply; the firewall unit only has to be enabled for boot
  systemctl enable tgpanel-firewall.service tgpanel.service >/dev/null 2>&1
  systemctl enable --now tgpanel-mtproxy-refresh.path >/dev/null 2>&1
  TGPANEL_BOOTSTRAP_PASSWORD="$PANEL_PASSWORD" run_ops bootstrap \
    "--domain=$PANEL_DOMAIN" "--login=$PANEL_LOGIN" "--admin-id=$ADMIN_ID" >/dev/null
  printf '%s\n' "$([ "$REF_GIVEN" = 1 ] && printf '%s' "$REF" || printf auto)" >"$STATE_DIR/ref"
}

offer_import() {
  log ""
  log "Остановите прежнего бота, иначе он перезапишет profiles.json своей версией."
  if [ "$ASSUME_YES" = 0 ]; then
    local _
    ask _ "Прежний бот остановлен? Нажмите Enter, чтобы продолжить... "
  fi
  log "Найденные профили и план импорта:"
  if ! "$CLI_LINK" import --dry-run; then
    warn "импорт сейчас невозможен; позже: tgpanel import"
    return 0
  fi
  local do_import=0
  case "$IMPORT_MODE" in
    yes) do_import=1 ;;
    no) do_import=0 ;;
    *) if confirm_strict "Импортировать эти профили сейчас?"; then do_import=1; fi ;;
  esac
  if [ "$do_import" = 1 ]; then
    "$CLI_LINK" import --yes || warn "импорт не выполнен (изменения откатены); повторите: tgpanel import"
  else
    log "Профили остались как есть и работают вне панели; импорт: tgpanel import"
  fi
}

install_caddy() {
  step "Шаг 9. Блок панели в Caddy и сертификат"
  local path_value domain_value rc=0
  path_value="${PANEL_PATH:-$(env_get TGPANEL_PANEL_PATH)}"
  domain_value="${PANEL_DOMAIN:-$(env_get TGPANEL_PANEL_DOMAIN)}"
  run_ops caddy-install "--domain=$domain_value" "--path=$path_value" --wait-cert=120 || rc=$?
  case "$rc" in
    0) ;;
    3) warn "сертификат пока не получен; Caddy продолжит попытки сам, панель заработает позже" ;;
    *) die "блок Caddy не установлен (Caddyfile возвращён к прежнему виду)" ;;
  esac
}

wait_panel_port() { # up to 20 s
  local i
  for ((i = 0; i < 40; i++)); do
    if (exec 3<>/dev/tcp/127.0.0.1/8090) 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  return 1
}

first_apply_and_start() {
  step "Шаг 10. Первое применение, запуск, самопроверка"
  local rc=0
  "$CLI_LINK" apply || rc=$?
  case "$rc" in
    0) ;;
    3) warn "apply отложен (см. сообщение выше): tgpanel apply --adopt или tgpanel import" ;;
    *) die "применение не удалось; изменения откатены" ;;
  esac
  systemctl restart tgpanel.service
  wait_panel_port || warn "панель не открыла порт 8090 за 20 секунд: journalctl -u tgpanel"
  if ! "$CLI_LINK" doctor; then
    warn "самопроверка нашла проблемы (см. выше). Повтор: tgpanel doctor; восстановление: tgpanel repair"
  fi
}

bot_name() {
  local token reply name=""
  token="${BOT_TOKEN:-$(env_get TGPANEL_BOT_TOKEN)}"
  reply="$(telegram_get_me "$token" || true)"
  if [ -n "$reply" ]; then
    name="$(printf '%s' "$reply" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
  fi
  printf '%s' "${name:+@}${name:-неизвестно}"
}

final_summary() {
  step "Шаг 11. Итог"
  local domain path login="" password=""
  domain="${PANEL_DOMAIN:-$(env_get TGPANEL_PANEL_DOMAIN)}"
  path="${PANEL_PATH:-$(env_get TGPANEL_PANEL_PATH)}"
  log "Адрес панели:   https://$domain/$path/"
  if [ -f "$CRED_FILE" ]; then
    login="$(sed -n 's/^login=//p' "$CRED_FILE" | head -n 1)"
    password="$(sed -n 's/^password=//p' "$CRED_FILE" | head -n 1)"
    log "Логин:          $login"
    log "Пароль:         $password"
    log "(пароль показывается в последний раз; новый: tgpanel reset-password)"
    rm -f "$CRED_FILE"
  else
    log "Логин и пароль не изменялись (адрес и логин: tgpanel show-url; новый пароль: tgpanel reset-password)"
  fi
  log "Бот:            $(bot_name)"
  if [ -z "$BACKUP_PATH" ]; then BACKUP_PATH="$(run_ops pre-install-backup)"; fi
  log "Исходная копия: $BACKUP_PATH"
  log "Диагностика:    tgpanel doctor    Обновление: tgpanel update    Удаление: tgpanel uninstall"
}

# --------------------------------------------------------------- update (re-run) flow

apply_changed_settings() {
  if [ -n "$PANEL_DOMAIN" ] && [ "$PANEL_DOMAIN" != "$STORED_DOMAIN" ]; then
    confirm "Сменить домен панели с $STORED_DOMAIN на $PANEL_DOMAIN? Блок Caddy будет заменён, понадобится новый сертификат" ||
      die "прервано: домен панели не изменён"
    env_set TGPANEL_PANEL_DOMAIN "$PANEL_DOMAIN"
    run_ops bootstrap "--domain=$PANEL_DOMAIN" >/dev/null
  fi
  if [ -n "$BOT_TOKEN" ] && [ "$BOT_TOKEN" != "$STORED_TOKEN" ]; then
    confirm "Заменить токен бота?" || die "прервано: токен бота не изменён"
    env_set TGPANEL_BOT_TOKEN "$BOT_TOKEN"
  fi
  if [ -n "$ADMIN_ID" ] && [ "$ADMIN_ID" != "$(env_get TGPANEL_ADMIN_IDS | cut -d, -f1)" ]; then
    warn "--admin-id при повторном запуске не меняется: администраторов ведите в панели"
  fi
}

update_flow() {
  [ -x "$VENV_PY" ] || die "найден $ENV_FILE, но нет $VENV_PY: переустановите (удалите $ETC_DIR только если уверены)"
  if [ -f "$CRED_FILE" ]; then
    warn "предыдущая установка не была завершена; доступы будут показаны в итоге"
  fi
  install_packages
  apply_changed_settings
  step "Обновление кода (так же, как tgpanel update)"
  if [ "$REF_GIVEN" = 1 ]; then
    run_ops update "--ref=$REF" || die "обновление не удалось (выполнен откат на прежнюю версию)"
  else
    run_ops update || die "обновление не удалось (выполнен откат на прежнюю версию)"
  fi
  install_caddy
  first_apply_and_start
  final_summary
}

fresh_flow() {
  install_packages
  fetch_code
  make_dirs_and_backup
  write_env
  install_units
  show_credentials_now
  offer_import
  install_caddy
  first_apply_and_start
  final_summary
}

acquire_install_lock() {
  command -v flock >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$LOCK_FILE")"
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "установщик уже запущен (блокировка $LOCK_FILE)"
}

# ------------------------------------------------------------------------------- main

main() {
  parse_args "$@"
  detect_tty
  check_system
  if [ -f "$ENV_FILE" ]; then UPDATE=1; fi
  check_proxy_installed # step 2 first, read-only; exits 1 on failure
  if [ "$UPDATE" = 1 ]; then
    STORED_DOMAIN="$(env_get TGPANEL_PANEL_DOMAIN)"
    STORED_TOKEN="$(env_get TGPANEL_BOT_TOKEN)"
    [ -n "$PANEL_DOMAIN" ] || PANEL_DOMAIN="$STORED_DOMAIN"
    [ -n "$BOT_TOKEN" ] || BOT_TOKEN="$STORED_TOKEN"
    [ -n "$ADMIN_ID" ] || ADMIN_ID="$(env_get TGPANEL_ADMIN_IDS | cut -d, -f1)"
    log "Найдена установка tgpanel: выполняется обновление"
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    preflight_checks
    log ""
    log "Проверки пройдены (--check-only): ничего не изменено."
    exit 0
  fi
  # No terminal: everything must be decided now, BEFORE any change.
  if [ "$HAVE_TTY" = 0 ]; then
    [ "$ASSUME_YES" = 1 ] || die "нет терминала (/dev/tty): добавьте --yes"
    if [ "$UPDATE" = 0 ]; then
      [ -n "$IMPORT_MODE" ] || die "нет терминала: укажите --import или --no-import"
      [ -n "$PANEL_DOMAIN" ] && [ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_ID" ] ||
        die "нет терминала: нужны --panel-domain, токен бота (--bot-token-file или TGPANEL_INSTALL_BOT_TOKEN) и --admin-id"
    fi
  fi
  acquire_install_lock
  [ -n "$PANEL_DOMAIN" ] || ask PANEL_DOMAIN "Домен панели (поддомен, не совпадающий с доменом прокси): "
  [ -n "$BOT_TOKEN" ] || ask BOT_TOKEN "Токен Telegram-бота: " secret
  [ -n "$ADMIN_ID" ] || ask ADMIN_ID "Telegram ID администратора: "
  validate_inputs
  preflight_checks
  if [ "$UPDATE" = 1 ]; then update_flow; else fresh_flow; fi
}

if [ "${TGPANEL_SOURCE_ONLY:-}" != 1 ]; then main "$@"; fi
