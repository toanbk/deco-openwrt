#!/usr/bin/env bash
# Serve an image to the Deco M5 bootloader over TFTP (macOS, needs sudo).
#
#   sudo scripts/flash-tftp.sh [image.bin]   # start serving, watch requests
#   sudo scripts/flash-tftp.sh --stop        # stop tftpd, put en5 back on DHCP
#
# The bootloader asks 192.168.0.66 for M5v1_tp_recovery.bin when reset is
# held during power-on. The same procedure restores TP-Link stock firmware.
set -euo pipefail

IFACE="${IFACE:-en5}"
SERVICE="${SERVICE:-AX88179B}"
TFTP_ROOT=/private/tftpboot
PLIST=/System/Library/LaunchDaemons/tftp.plist
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }

if [ "${1:-}" = "--stop" ]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$TFTP_ROOT/M5v1_tp_recovery.bin" "$TFTP_ROOT/m5v1_tp_recovery.bin"
  networksetup -setdhcp "$SERVICE"
  echo "tftpd stopped, $SERVICE back on DHCP. OpenWrt answers on http://192.168.1.1"
  echo "No 192.168.1.x lease within a minute? The cable is on the OpenWrt WAN port: move it to the other port."
  exit 0
fi

IMAGE="${1:-$ROOT/firmware/tplink_deco-m5-v3-factory.bin}"
[ -f "$IMAGE" ] || { echo "Image not found: $IMAGE (run scripts/build-firmware.sh)"; exit 1; }

networksetup -setmanual "$SERVICE" 192.168.0.66 255.255.255.0
mkdir -p "$TFTP_ROOT"
# Some v3 units request the lowercase name; the default case-insensitive
# APFS volume serves both spellings from this one file.
rm -f "$TFTP_ROOT/M5v1_tp_recovery.bin" "$TFTP_ROOT/m5v1_tp_recovery.bin"
cp "$IMAGE" "$TFTP_ROOT/M5v1_tp_recovery.bin"
[ -e "$TFTP_ROOT/m5v1_tp_recovery.bin" ] || cp "$IMAGE" "$TFTP_ROOT/m5v1_tp_recovery.bin"
chmod 644 "$TFTP_ROOT/M5v1_tp_recovery.bin" "$TFTP_ROOT/m5v1_tp_recovery.bin"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -F "$PLIST"

cat <<EOF
Serving $(basename "$IMAGE") from $TFTP_ROOT on $IFACE (192.168.0.66).
1. Cable in the Deco port furthest from the power jack.
2. Unplug power, hold reset, plug power in, keep holding until a
   'RRQ ... m5v1_tp_recovery.bin' line appears below, then release.
3. Wait for the LED to settle (2-3 min). Do not cut power.
4. Ctrl-C here, then: sudo $0 --stop
EOF
tcpdump -lni "$IFACE" udp port 69 or icmp or arp
