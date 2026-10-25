#!/usr/bin/env bash
# Generate the config from proxies.csv and push it to the router.
#
#   scripts/deploy.sh                # deploy to root@10.10.10.3 (WAN, static lease on MikroTik)
#   ROUTER=192.168.1.1 scripts/deploy.sh   # over the Deco LAN port instead
#   scripts/deploy.sh --check        # only show status on the router
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ROUTER="${ROUTER:-10.10.10.3}"
COUNTRY="${COUNTRY:-VN}"
# Upstream networks allowed to reach SSH (key only) and LuCI through WAN; empty disables.
ADMIN_FROM="${ADMIN_FROM-10.10.10.0/24 192.168.68.0/24 192.168.88.0/24}"
SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "root@$ROUTER")

check() {
  "${SSH[@]}" sh -s <<'EOF'
echo "== board";      ubus call system board | grep -E '"(model|release)"|"description"' | head -3
echo "== overlay";    df -h /overlay | tail -1
echo "== instances";  echo "hev running: $(pidof hev-socks5-tproxy | wc -w), configs: $(ls /etc/socks-ssid/px*.yml | wc -l)"
echo "== listening";  netstat -lnp 2>/dev/null | grep hev-socks5 | awk '{print $1, $4}' | sort | tr '\n' ' '; echo
echo "== ip rule";    ip rule | grep -i 'lookup 100' || echo "MISSING fwmark rule"
echo "== table 100";  ip route show table 100 || true
echo "== nft";        nft list chain inet fw4 socks_ssid_prerouting 2>/dev/null | grep -c tproxy | sed 's/^/tproxy rules: /'
echo "== firewall";   echo "tproxy accept rules: $(fw4 print 2>/dev/null | grep -c 'px[0-9]*-tproxy')"
echo "== wan";        ifstatus wan | jsonfilter -e '@["ipv4-address"][0].address' || echo "no WAN address"
echo "== ssids";      iwinfo 2>/dev/null | awk '/ESSID/ {e=$0} /Mode: Master/ {print e}'
echo "== logs";       logread -e hev-socks5 2>/dev/null | tail -10
EOF
}

if [ "${1:-}" = "--check" ]; then check; exit 0; fi

python3 -I "$ROOT/scripts/generate-config.py" --country "$COUNTRY" --admin-from "$ADMIN_FROM"
mkdir -p "$ROOT/build/rootfs/usr/sbin"
install -m 755 "$ROOT/router/usr/sbin/socks-ssid-apply" "$ROOT/build/rootfs/usr/sbin/socks-ssid-apply"

echo "Uploading to $ROUTER ..."
COPYFILE_DISABLE=1 tar -C "$ROOT/build/rootfs" --no-xattrs -cf - . |
  "${SSH[@]}" 'rm -rf /etc/socks-ssid && tar -C / -xf - && /usr/sbin/socks-ssid-apply'

echo "Waiting for Wi-Fi to settle ..."
sleep 15
check
