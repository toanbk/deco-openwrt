#!/usr/bin/env bash
# Join every SSID in proxies.csv with the Mac's Wi-Fi and compare the
# public IP seen through the SSID with the IP seen when the Mac talks to
# the same SOCKS5 proxy directly. The Mac loses its normal Wi-Fi while this
# runs; at the end Wi-Fi is power-cycled so macOS rejoins its usual network.
#
#   scripts/test-ssids-from-mac.sh [id ...]   # default: every row
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CSV="${CSV:-$ROOT/proxies.csv}"
WIFI="${WIFI:-en0}"
IP_URL="https://api.ipify.org"

rows=$(python3 -I - "$CSV" "$@" <<'EOF'
import csv, sys
want = set(sys.argv[2:])
lines = [l for l in open(sys.argv[1], encoding="utf-8") if l.strip() and not l.lstrip().startswith("#")]
for r in csv.DictReader(lines):
    if not want or r["id"].strip() in want:
        auth = f'{r["proxy_user"]}:{r["proxy_pass"]}@' if r["proxy_user"].strip() else ""
        print("\t".join([r["id"].strip(), r["ssid"].strip(), r["wifi_key"].strip(),
                         f'socks5h://{auth}{r["proxy_host"].strip()}:{r["proxy_port"].strip()}']))
EOF
)
[ -n "$rows" ] || { echo "no rows selected"; exit 1; }

pass=0; fail=0
while IFS=$'\t' read -r id ssid key proxy; do
  direct=$(curl -s -m 20 -x "$proxy" "$IP_URL" || true)
  networksetup -setairportnetwork "$WIFI" "$ssid" "$key" >/dev/null 2>&1
  ip=""
  for _ in $(seq 1 20); do
    ip=$(ipconfig getifaddr "$WIFI" 2>/dev/null || true)
    [[ "$ip" == 172.20.$id.* ]] && break
    sleep 1
  done
  if [[ "$ip" != 172.20.$id.* ]]; then
    echo "px$id $ssid: FAIL did not get a 172.20.$id.x lease (got '${ip:-none}')"
    fail=$((fail + 1)); continue
  fi
  via=$(curl -s -m 20 --interface "$WIFI" "$IP_URL" || true)
  v6=$(curl -s -6 -m 5 --interface "$WIFI" https://api64.ipify.org || true)
  status=PASS
  [ -n "$via" ] && [ "$via" = "$direct" ] || status=FAIL
  [ -z "$v6" ] || status=FAIL
  echo "px$id $ssid: $status lease=$ip via-ssid=${via:-none} direct-proxy=${direct:-none} ipv6=${v6:-blocked}"
  [ "$status" = PASS ] && pass=$((pass + 1)) || fail=$((fail + 1))
done <<< "$rows"

networksetup -setairportpower "$WIFI" off; sleep 2; networksetup -setairportpower "$WIFI" on
echo "passed=$pass failed=$fail (rotating proxies can show different IPs on each request)"
[ "$fail" = 0 ]
