#!/usr/bin/env bash
# tgpanel server self-check (PLAN phase 8). Run as root ON THE SERVER, ideally BEFORE or right
# after installing tgpanel. Read-mostly: it never changes proxy files, profiles, units or the
# Caddyfile permanently. Everything it creates uses the unique prefix below (a throwaway nft
# table, temp files, throwaway MTProxy processes, one throwaway unit file) and is removed by a
# trap, also when the script is interrupted.
#
# Items and what each check proves (numbers follow PLAN phase 8):
#   1  MTProxy accepts TCP connections on 127.64.x.y:<port> (it binds 0.0.0.0, so any 127/8
#      address works) and `tproxy-server -check` accepts such backends.
#   2  nft per-element set counters exist and grow with loopback traffic (`nc` over lo), so
#      traffic accounting by destination address works on this kernel/nftables.
#   3  Two addresses served by ONE MTProxy process are counted separately.
#   4  A process with 16 secrets (-S x16) starts and stays up; a 17th is tried and only
#      reported (decides whether secrets_per_process must drop to 15).
#   8  Memory (RSS) per MTProxy process with 1 and 16 secrets -> estimate for 20 pools.
#   -  `systemctl mask` on a regular unit file (throwaway unit): shows whether `legacy-mtproxy
#      off` must use its drop-in fallback.
#   -  `nft list set` on a missing table: confirms the error behaviour the pipeline relies on.
#   14 Panel certificate: issuer/dates from the live TLS handshake (run the installer twice and
#      compare "not before" to confirm that the second run did not request a new certificate).
# Needing a Telegram client (printed as instructions): 5, 6, 7, 12, 13; plus 9 (forced apply
# failure), 10 (reboot) and 11 (uninstall) which change the server and are done by hand.
set -uo pipefail

PREFIX="tgpanel-selfcheck-$$"
MTPROXY_BIN="${MTPROXY_BIN:-/opt/MTProxy/objs/bin/mtproto-proxy}"
TPROXY_BIN="${TPROXY_BIN:-/usr/local/bin/tproxy-server}"
MTPROXY_USER="${MTPROXY_USER:-mtproxy}"
AES_PWD="${AES_PWD:-/etc/mtproxy/proxy-secret}"
MULTI_CONF="${MULTI_CONF:-/etc/mtproxy/proxy-multi.conf}"
NFT_TABLE="tgpanel_selfcheck"
ENV_FILE="${ENV_FILE:-/etc/tgpanel/tgpanel.env}"
WORKDIR=""
PIDS=()
UNIT_FILE=""
NAMES=()
STATES=()
DETAILS=()

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
  if [ -n "$UNIT_FILE" ]; then
    systemctl unmask "$(basename "$UNIT_FILE")" >/dev/null 2>&1 || true
    rm -f "$UNIT_FILE"
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if [ -n "$WORKDIR" ]; then rm -rf "$WORKDIR"; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

need_root() {
  [ "$(id -u)" = 0 ] || {
    echo "Запустите от root." >&2
    exit 2
  }
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

# start_proxy <name> <port> <stats_port> <secret_count> -> pid in PROXY_PID, log in $WORKDIR/<name>.log
start_proxy() {
  local name="$1" port="$2" stats="$3" count="$4" args=() i
  for ((i = 0; i < count; i++)); do args+=(-S "$(hex_secret)"); done
  "$MTPROXY_BIN" -u "$MTPROXY_USER" -p "$stats" -H "$port" "${args[@]}" \
    --aes-pwd "$AES_PWD" "$MULTI_CONF" -M 1 -C 256 >"$WORKDIR/$name.log" 2>&1 &
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
  local port stats
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
  [ -n "${MAIN_PORT:-}" ] || {
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
  local port stats pid
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
  local p17 s17
  p17="$(free_port 2480 2489)" && s17="$(free_port 8980 8989)" || return 0
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
  [ -n "${MAIN_PID:-}" ] && one="$(ps -o rss= -p "$MAIN_PID" 2>/dev/null | tr -d ' ')"
  [ -n "${SIXTEEN_PID:-}" ] && sixteen="$(ps -o rss= -p "$SIXTEEN_PID" 2>/dev/null | tr -d ' ')"
  if [ -n "$one" ]; then
    info "8 память на процесс MTProxy" "1 секрет: $((one / 1024)) МиБ${sixteen:+, 16 секретов: $((sixteen / 1024)) МиБ}; для 20 пулов ≈ $((${sixteen:-$one} * 20 / 1024)) МиБ"
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

check_14_certificate() {
  local domain=""
  [ -r "$ENV_FILE" ] && domain="$(sed -n 's/^TGPANEL_PANEL_DOMAIN=//p' "$ENV_FILE" | head -n 1)"
  if [ -z "$domain" ] || ! command -v openssl >/dev/null 2>&1; then
    info "14 сертификат панели" "tgpanel ещё не установлен или нет openssl"
    return
  fi
  local dates issuer
  dates="$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null | openssl x509 -noout -startdate -enddate 2>/dev/null | tr '\n' ' ')"
  issuer="$(echo | openssl s_client -connect "$domain:443" -servername "$domain" 2>/dev/null | openssl x509 -noout -issuer 2>/dev/null)"
  if [ -n "$dates" ]; then
    info "14 сертификат панели" "$issuer; $dates (повторный запуск установщика не должен менять notBefore)"
  else
    fail "14 сертификат панели" "TLS-рукопожатие с $domain не удалось"
  fi
}

print_manual() {
  cat <<'EOF'

Ручные проверки (нужен клиент Telegram или изменение сервера):
  5   Выключите пользователя в панели -> клиент не подключается; включите -> подключается
      без перезапуска пула (journalctl -u 'tgpanel-mtproxy@*' не показывает рестарта).
  6   Перезапустите relay (systemctl restart tproxy-server) с несколькими подключёнными
      клиентами: засеките время восстановления, проверьте счётчик tproxy_limit_hits_total
      на 127.0.0.1:8081/metrics (упора в burst-лимиты быть не должно).
  7   Откройте ссылку https://t.me/webproxy?server=<хост>&secret=<секрет> и tg://webproxy?...
      в клиенте: какой формат открывается; результат запишите в настройки/README.
  9   Искусственный сбой apply: временно испортите шаблон (например, tgpanel apply при
      недоступном порту) -> панель откатывается, прокси продолжает работать.
  10  Перезагрузите сервер: tgpanel doctor без ошибок, счётчики не дают ложных всплесков.
  11  tgpanel uninstall возвращает сервер к состоянию pre-install (профили, Caddyfile).
  12  Импорт 15 профилей user_<id>: Telegram ID распознаны, старые ссылки работают,
      трафик считается, у пользователей комментарий «import».
  13  Создайте пользователя: замерьте время от «Создать» до рабочей ссылки.
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
  WORKDIR="$(mktemp -d "/tmp/${PREFIX}.XXXXXX")"
  chmod 0755 "$WORKDIR"
  MAIN_PID="" MAIN_PORT="" SIXTEEN_PID=""
  if check_prereqs; then
    check_1_loopback_backend
    check_2_3_nft_counters
    check_4_secret_limit
    check_8_memory
  fi
  check_mask_regular_unit
  check_nft_missing_table
  check_14_certificate
  print_manual
  summary
}

main "$@"
