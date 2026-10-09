#!/usr/bin/env bash
# tgpanel server self-check (PLAN phase 8). Run as root ON THE SERVER, ideally BEFORE or right
# after installing tgpanel. Read-mostly: it never changes proxy files, profiles, units or the
# Caddyfile permanently. Everything it creates uses the unique prefix below (throwaway nft
# tables, temp files, throwaway MTProxy processes, one throwaway unit file) and is removed by a
# trap on EXIT/INT/TERM/HUP.
#
# Items and what each check proves (numbers follow PLAN phase 8):
#   1  MTProxy accepts TCP connections on 127.64.x.y:<port> (it binds 0.0.0.0, so any 127/8
#      address works) and `tproxy-server -check` accepts such backends.
#   2  nft per-element set counters exist and grow with loopback traffic (`nc` over lo), so
#      traffic accounting by destination address works on this kernel/nftables.
#   3  Two addresses served by ONE MTProxy process are counted separately.
#   4  A process with 16 secrets (-S x16) starts and stays up; a 17th is tried and only
#      reported (decides whether secrets_per_process must drop to 15).
#   8  Memory (RSS) per idle MTProxy process (1 and 16 secrets, real -C) -> lower estimate for
#      20 pools (grows with clients).
#   -  `systemctl mask` on a regular unit file (throwaway unit): shows whether `legacy-mtproxy
#      off` must use its drop-in fallback.
#   -  `nft list set` on a missing table: confirms the error behaviour the pipeline relies on.
#   -  `caddy validate` with an unwritable HOME (tgpanel.service runs with ProtectHome).
#   14 Panel certificate: notBefore is saved in /var/lib/tgpanel/selfcheck-cert-notbefore and
#      compared between runs (run: script, install.sh again, script -> must be unchanged).
# Temporary MTProxy ports are protected by a throwaway nft table that drops non-loopback
# traffic to them, and the processes inherit MTPROXY_NAT_ARGS like the real unit.
# Needing a Telegram client (printed as instructions): 4 (client via secret #1 and #16), 5, 6,
# 7, 12, 13; plus 9 (safe apply-failure procedure), 10 (reboot) and 11 (uninstall).
set -uo pipefail

PREFIX="tgpanel-selfcheck-$$"
MTPROXY_BIN="${MTPROXY_BIN:-/opt/MTProxy/objs/bin/mtproto-proxy}"
TPROXY_BIN="${TPROXY_BIN:-/usr/local/bin/tproxy-server}"
MTPROXY_USER="${MTPROXY_USER:-mtproxy}"
AES_PWD="${AES_PWD:-/etc/mtproxy/proxy-secret}"
MULTI_CONF="${MULTI_CONF:-/etc/mtproxy/proxy-multi.conf}"
MTPROXY_ENV="${MTPROXY_ENV:-/etc/mtproxy/mtproxy.env}"
MAX_CONN="${SELFCHECK_MAX_CONN:-4096}"
NFT_TABLE="tgpanel_selfcheck_$$"
GUARD_TABLE="tgpanel_selfcheck_guard_$$"
ENV_FILE="${ENV_FILE:-/etc/tgpanel/tgpanel.env}"
CERT_STATE="${CERT_STATE:-/var/lib/tgpanel/selfcheck-cert-notbefore}"
WORKDIR=""
PIDS=()
NAT_ARGS=()
UNIT_FILE=""
NAMES=()
STATES=()
DETAILS=()
PROXY_PID=""
MAIN_PID=""
MAIN_PORT=""
SIXTEEN_PID=""

record() { # status name detail
  STATES+=("$1")
  NAMES+=("$2")
  DETAILS+=("${3:-}")
  printf '[%s] %s %s\n' "$1" "$2" "${3:+- $3}"
}
pass() { record PASS "$@"; }
fail() { record FAIL "$@"; }
info() { record INFO "$@"; }

cleanup() {
  local pid
  if [ "${#PIDS[@]}" -gt 0 ]; then
    for pid in "${PIDS[@]}"; do
      kill "$pid" >/dev/null 2>&1 || true
    done
  fi
  nft delete table inet "$NFT_TABLE" >/dev/null 2>&1 || true
  nft delete table inet "$GUARD_TABLE" >/dev/null 2>&1 || true
  if [ -n "$UNIT_FILE" ]; then
    systemctl unmask "$(basename "$UNIT_FILE")" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -n "$WORKDIR" ]; then rm -rf "$WORKDIR"; fi
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

need_root() {
  [ "$(id -u)" = 0 ] || {
    echo "Запустите от root." >&2
    exit 2
  }
  local tool
  for tool in nft nc ss ps awk curl; do
    command -v "$tool" >/dev/null 2>&1 || {
      echo "Не найден инструмент: $tool" >&2
      exit 2
    }
  done
}

free_port() { # first last -> a port nobody listens on
  local p
  for p in $(seq "$1" "$2"); do
    if ! ss -Hltn | awk '{print $4}' | grep -q ":$p\$"; then
      printf '%s' "$p"
      return 0
    fi
  done
  return 1
}

hex_secret() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# start_proxy <name> <port> <stats_port> <secret_count> -> pid in PROXY_PID
start_proxy() {
  local name="$1" port="$2" stats="$3" count="$4" args=() i
  for ((i = 0; i < count; i++)); do args+=(-S "$(hex_secret)"); done
  "$MTPROXY_BIN" -u "$MTPROXY_USER" -p "$stats" -H "$port" "${args[@]}" \
    ${NAT_ARGS[@]+"${NAT_ARGS[@]}"} \
    --aes-pwd "$AES_PWD" "$MULTI_CONF" -M 1 -C "$MAX_CONN" >"$WORKDIR/$name.log" 2>&1 &
  PROXY_PID=$!
  PIDS+=("$PROXY_PID")
}

wait_listen() { # port seconds
  local i
  for ((i = 0; i < $2 * 5; i++)); do
    if ss -Hltn | awk '{print $4}' | grep -q ":$1\$"; then return 0; fi
    sleep 0.2
  done
  return 1
}

counter_bytes() { # set ip -> bytes counter of an element
  nft list set inet "$NFT_TABLE" "$1" 2>/dev/null |
    tr ',' '\n' | awk -v ip="$2" '$0 ~ ip {f=1} f && /bytes/ {for(i=1;i<=NF;i++) if($i=="bytes"){print $(i+1); exit}}'
}

send_traffic() { # ip port
  printf 'tgpanel-selfcheck-payload' | nc -w 1 "$1" "$2" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------------- checks

load_nat_args() { # inherit MTPROXY_NAT_ARGS exactly like the real unit does
  local raw="${MTPROXY_NAT_ARGS:-}"
  if [ -z "$raw" ] && [ -r "$MTPROXY_ENV" ]; then
    raw="$(sed -n 's/^MTPROXY_NAT_ARGS=//p' "$MTPROXY_ENV" | head -n 1 | tr -d '"')"
  fi
  if [ -n "$raw" ]; then
    read -r -a NAT_ARGS <<<"$raw"
    info "NAT-аргументы MTProxy" "унаследованы: ${NAT_ARGS[*]}"
  fi
}

# Temporary ports of the throwaway MTProxy processes must not be reachable from outside.
install_guard() {
  local ports="2470-2499, 8970-8999"
  if nft -f - >/dev/null 2>&1 <<EOF
table inet $GUARD_TABLE {
	chain guard {
		type filter hook input priority -10; policy accept;
		iifname != "lo" tcp dport { $ports } drop
	}
}
EOF
  then
    pass "защита временных портов" "таблица $GUARD_TABLE закрывает $ports снаружи"
  else
    fail "защита временных портов" "не удалось создать таблицу nft: проверки с MTProxy пропущены"
    return 1
  fi
}

check_prereqs() {
  local missing=""
  [ -x "$MTPROXY_BIN" ] || missing="$missing $MTPROXY_BIN"
  [ -r "$AES_PWD" ] || missing="$missing $AES_PWD"
  [ -r "$MULTI_CONF" ] || missing="$missing $MULTI_CONF"
  if [ -n "$missing" ]; then
    fail "предпосылки" "нет файлов:$missing"
    return 1
  fi
  pass "предпосылки" "бинарник MTProxy и данные Telegram на месте"
}

check_1_loopback_backend() {
  local port stats out
  port="$(free_port 2470 2489)" || port=""
  stats="$(free_port 8970 8989)" || stats=""
  if [ -z "$port" ] || [ -z "$stats" ]; then
    fail "1 MTProxy на 127.64.x.y" "не нашёл свободные порты"
    return
  fi
  start_proxy one "$port" "$stats" 1
  MAIN_PID="$PROXY_PID"
  MAIN_PORT="$port"
  if ! wait_listen "$port" 8; then
    fail "1 MTProxy на 127.64.x.y" "процесс не открыл порт $port: $(tail -n 3 "$WORKDIR/one.log" | tr '\n' ' ')"
    return
  fi
  if nc -z -w 2 127.64.9.9 "$port"; then
    pass "1 MTProxy принимает соединения на 127.64.9.9:$port"
  else
    fail "1 MTProxy на 127.64.x.y" "соединение с 127.64.9.9:$port не принято"
  fi
  # relay -check with such a backend (temp copies, nothing installed)
  if [ -x "$TPROXY_BIN" ] && [ -r /etc/tproxy-server/config.json ]; then
    cp /etc/tproxy-server/config.json "$WORKDIR/config.json"
    (umask 077 && printf '{"profiles":[{"name":"u1","secret":"%s","backend":"127.64.9.9:%s","carrier_mode":"https"}]}\n' \
      "$(hex_secret)" "$port" >"$WORKDIR/profiles.json")
    if out="$("$TPROXY_BIN" -config "$WORKDIR/config.json" -profiles-file "$WORKDIR/profiles.json" -check 2>&1)"; then
      pass "1 tproxy-server -check с backend 127.64.9.9"
    else
      fail "1 tproxy-server -check с backend 127.64.9.9" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-200)"
    fi
  else
    info "1 tproxy-server -check" "relay или config.json не найдены, пропущено"
  fi
}

check_2_3_nft_counters() {
  [ -n "$MAIN_PORT" ] || {
    info "2/3 счётчики nft" "нет рабочего MTProxy из проверки 1"
    return
  }
  local nftfile="$WORKDIR/probe.nft"
  cat >"$nftfile" <<EOF
table inet $NFT_TABLE {
	set up {
		type ipv4_addr
		counter
		elements = { 127.64.9.9, 127.64.9.8 }
	}
	chain acct {
		type filter hook output priority 0; policy accept;
		oifname "lo" meta l4proto tcp ip daddr @up
	}
}
EOF
  if ! nft -f "$nftfile" 2>"$WORKDIR/nft.err"; then
    fail "2 счётчики на элементах наборов nft" "$(tr '\n' ' ' <"$WORKDIR/nft.err" | cut -c1-200)"
    return
  fi
  local b9 b8
  send_traffic 127.64.9.9 "$MAIN_PORT"
  sleep 0.3
  b9="$(counter_bytes up 127.64.9.9)"
  b8="$(counter_bytes up 127.64.9.8)"
  if [ "${b9:-0}" -gt 0 ] 2>/dev/null; then
    pass "2 счётчик элемента растёт от трафика через lo" "127.64.9.9: ${b9} байт"
  else
    fail "2 счётчик элемента растёт от трафика через lo" "значение: '${b9:-пусто}'"
  fi
  if [ "${b8:-0}" = 0 ] 2>/dev/null; then
    pass "3 соседний адрес не получил чужой трафик" "127.64.9.8: 0 байт"
  else
    fail "3 два адреса в одном процессе" "127.64.9.8 получил ${b8} байт без своего трафика"
  fi
  send_traffic 127.64.9.8 "$MAIN_PORT"
  sleep 0.3
  b8="$(counter_bytes up 127.64.9.8)"
  if [ "${b8:-0}" -gt 0 ] 2>/dev/null; then
    pass "3 два адреса одного процесса считаются раздельно" "127.64.9.8: ${b8} байт"
  else
    fail "3 два адреса одного процесса считаются раздельно" "127.64.9.8 не вырос"
  fi
  nft delete table inet "$NFT_TABLE" >/dev/null 2>&1 || true
}

check_4_secret_limit() {
  local port stats pid p17 s17
  port="$(free_port 2490 2499)" || port=""
  stats="$(free_port 8990 8999)" || stats=""
  if [ -z "$port" ] || [ -z "$stats" ]; then
    fail "4 предел секретов" "нет свободных портов"
    return
  fi
  start_proxy s16 "$port" "$stats" 16
  pid="$PROXY_PID"
  sleep 3
  if kill -0 "$pid" 2>/dev/null && wait_listen "$port" 3; then
    pass "4 процесс с 16 секретами запущен и жив через 3 с"
    SIXTEEN_PID="$pid"
  else
    fail "4 процесс с 16 секретами" "не живёт: поставьте secrets_per_process = 15; $(tail -n 3 "$WORKDIR/s16.log" | tr '\n' ' ')"
  fi
  p17="$(free_port 2480 2489)" || return 0
  s17="$(free_port 8980 8989)" || return 0
  start_proxy s17 "$p17" "$s17" 17
  pid="$PROXY_PID"
  sleep 3
  if kill -0 "$pid" 2>/dev/null && wait_listen "$p17" 3; then
    info "4 17 секретов в одном процессе" "процесс ЖИВ (предел мягкий); оставляем 16 по документации"
  else
    info "4 17 секретов в одном процессе" "процесс не стартовал или упал: предел 16 подтверждён"
  fi
}

check_8_memory() {
  local one="" sixteen=""
  [ -n "$MAIN_PID" ] && one="$(ps -o rss= -p "$MAIN_PID" 2>/dev/null | tr -d ' ')"
  [ -n "$SIXTEEN_PID" ] && sixteen="$(ps -o rss= -p "$SIXTEEN_PID" 2>/dev/null | tr -d ' ')"
  if [ -n "$one" ]; then
    info "8 память на процесс MTProxy" "1 секрет: $((one / 1024)) МиБ${sixteen:+, 16 секретов: $((sixteen / 1024)) МиБ} (при -C $MAX_CONN, без клиентов); для 20 пулов ≈ $((${sixteen:-$one} * 20 / 1024)) МиБ — нижняя оценка, с подключёнными клиентами память растёт"
  else
    info "8 память на процесс MTProxy" "нет данных"
  fi
}

check_mask_regular_unit() {
  UNIT_FILE="/etc/systemd/system/${PREFIX}.service"
  printf '[Service]\nExecStart=/bin/true\n' >"$UNIT_FILE"
  systemctl daemon-reload >/dev/null 2>&1
  local out
  if out="$(systemctl mask "$(basename "$UNIT_FILE")" 2>&1)"; then
    pass "mask обычного юнита из /etc/systemd/system" "systemctl mask сработал"
  else
    info "mask обычного юнита из /etc/systemd/system" "mask отказал (ожидаемо): legacy-mtproxy off использует drop-in. $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-160)"
  fi
  systemctl unmask "$(basename "$UNIT_FILE")" >/dev/null 2>&1 || true
  rm -f "$UNIT_FILE"
  UNIT_FILE=""
  systemctl daemon-reload >/dev/null 2>&1
}

check_nft_missing_table() {
  local out rc=0
  out="$(nft list set inet "${NFT_TABLE}_missing" up 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    pass "nft list set без таблицы завершается ошибкой" "код $rc: $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-120)"
  else
    fail "nft list set без таблицы" "неожиданно успешно"
  fi
}

# caddy validate must work with an unwritable HOME (tgpanel.service runs with ProtectHome).
check_caddy_validate_unwritable_home() {
  local caddy=/usr/local/bin/caddy envargs=() f line kv
  if [ ! -x "$caddy" ]; then
    info "caddy validate без доступа к HOME" "нет $caddy"
    return
  fi
  for f in /etc/systemd/system/caddy.service.d/*.conf; do
    [ -f "$f" ] || continue
    while IFS= read -r line; do
      kv="${line#Environment=}"
      kv="${kv//\"/}"
      envargs+=("$kv")
    done < <(grep '^Environment=' "$f" || true)
  done
  mkdir -p "$WORKDIR/xdg"
  if env -i PATH=/usr/bin:/bin HOME=/nonexistent-selfcheck ${envargs[@]+"${envargs[@]}"} \
    "$caddy" validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$WORKDIR/caddy1.log" 2>&1; then
    pass "caddy validate при недоступном HOME" "работает и без XDG-каталогов"
  else
    info "caddy validate при недоступном HOME без XDG" "не работает, нужны XDG_*_HOME: $(tail -n 2 "$WORKDIR/caddy1.log" | tr '\n' ' ' | cut -c1-160)"
    if env -i PATH=/usr/bin:/bin HOME=/nonexistent-selfcheck XDG_DATA_HOME="$WORKDIR/xdg" \
      XDG_CONFIG_HOME="$WORKDIR/xdg" ${envargs[@]+"${envargs[@]}"} \
      "$caddy" validate --config /etc/caddy/Caddyfile --adapter caddyfile >"$WORKDIR/caddy2.log" 2>&1; then
      pass "caddy validate с XDG_DATA_HOME/XDG_CONFIG_HOME" "работает (так его вызывает tgpanel)"
    else
      fail "caddy validate с XDG-каталогами" "$(tail -n 2 "$WORKDIR/caddy2.log" | tr '\n' ' ' | cut -c1-160)"
    fi
  fi
}

check_14_certificate() {
  local domain=""
  [ -r "$ENV_FILE" ] && domain="$(sed -n 's/^TGPANEL_PANEL_DOMAIN=//p' "$ENV_FILE" | head -n 1)"
  if [ -z "$domain" ] || ! command -v openssl >/dev/null 2>&1; then
    info "14 сертификат панели" "tgpanel ещё не установлен или нет openssl"
    return
  fi
  local cert notbefore notafter issuer previous
  cert="$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null |
    openssl x509 -noout -startdate -enddate -issuer 2>/dev/null || true)"
  if [ -z "$cert" ]; then
    fail "14 сертификат панели" "TLS-рукопожатие с $domain не удалось"
    return
  fi
  notbefore="$(printf '%s\n' "$cert" | sed -n 's/^notBefore=//p')"
  notafter="$(printf '%s\n' "$cert" | sed -n 's/^notAfter=//p')"
  issuer="$(printf '%s\n' "$cert" | sed -n 's/^issuer=//p')"
  if [ -r "$CERT_STATE" ]; then
    previous="$(cat "$CERT_STATE")"
    if [ "$previous" = "$notbefore" ]; then
      pass "14 сертификат не перевыпускался между запусками" "notBefore=$notbefore; издатель: $issuer; до $notafter"
    else
      info "14 сертификат изменился с прошлого запуска" "было notBefore=$previous, стало $notbefore (ожидаемо при продлении; НЕ ожидаемо после повторного запуска установщика)"
    fi
  else
    info "14 сертификат панели" "первый запуск, запомнил notBefore=$notbefore (издатель: $issuer; до $notafter)"
  fi
  if mkdir -p "$(dirname "$CERT_STATE")" 2>/dev/null; then
    (umask 077 && printf '%s' "$notbefore" >"$CERT_STATE")
  fi
}

print_manual() {
  cat <<'EOF'

Ручные проверки (нужен клиент Telegram или изменение сервера):
  4   Предел секретов, как он проявился у владельца: создайте пул с 16 пользователями, затем
      подключите клиент Telegram через секрет №1 и через секрет №16 этого пула. Оба должны
      работать. Если №16 (или процесс целиком) не работает — поставьте в настройках панели
      secrets_per_process = 15.
  5   Выключите пользователя в панели -> клиент не подключается; включите -> подключается
      без перезапуска пула (journalctl -u 'tgpanel-mtproxy@*' не показывает рестарта).
  6   Перезапустите relay (systemctl restart tproxy-server) с несколькими подключёнными
      клиентами: засеките время восстановления, проверьте счётчик tproxy_limit_hits_total
      на 127.0.0.1:8081/metrics (упора в burst-лимиты быть не должно).
  7   Откройте ссылку https://t.me/webproxy?server=<хост>&secret=<секрет> и tg://webproxy?...
      в клиенте: какой формат открывается; результат запишите в README.
  9   Искусственный сбой apply (безопасно): создайте в панели тестового пользователя, затем
        chattr +i /etc/tproxy-server/profiles.json
      выполните  tgpanel apply  (или создайте ещё одного тестового пользователя) — ожидается
      ОШИБКА, откат, relay и существующие ссылки продолжают работать; затем
        chattr -i /etc/tproxy-server/profiles.json
      и удалите тестовых пользователей. Не оставляйте флаг +i: он блокирует работу панели.
  10  Перезагрузите сервер: tgpanel doctor без ошибок, счётчики не дают ложных всплесков.
  11  tgpanel uninstall возвращает сервер к состоянию pre-install (профили, Caddyfile).
  12  Импорт 15 профилей user_<id>: Telegram ID распознаны, старые ссылки работают,
      трафик считается, у пользователей комментарий «import».
  13  Создайте пользователя: замерьте время от «Создать» до рабочей ссылки.
  14  Запустите этот скрипт, затем повторно install.sh, затем скрипт ещё раз: notBefore
      сертификата не должен измениться (скрипт сам сравнивает со значением из
      /var/lib/tgpanel/selfcheck-cert-notbefore).
EOF
}

summary() {
  local i failed=0
  printf '\n==== Итог ====\n'
  for i in "${!NAMES[@]}"; do
    printf '%-5s %s\n' "${STATES[$i]}" "${NAMES[$i]}${DETAILS[$i]:+ — ${DETAILS[$i]}}"
    [ "${STATES[$i]}" = FAIL ] && failed=$((failed + 1))
  done
  printf '\nПровалено проверок: %d\n' "$failed"
  [ "$failed" -eq 0 ]
}

main() {
  need_root
  WORKDIR="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")" # mktemp -d is 0700
  if check_prereqs && install_guard; then
    load_nat_args
    check_1_loopback_backend
    check_2_3_nft_counters
    check_4_secret_limit
    check_8_memory
  fi
  check_mask_regular_unit
  check_nft_missing_table
  check_caddy_validate_unwritable_home
  check_14_certificate
  print_manual
  summary
}

main "$@"
