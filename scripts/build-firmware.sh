#!/usr/bin/env bash
# Build a custom OpenWrt image for the Deco M5 through the official
# Attended SysUpgrade service, download it and verify its sha256.
# The image ships every package the socks-ssid setup needs, so no kmod
# has to be installed later against a moving snapshot kernel.
set -euo pipefail

ASU="${ASU:-https://sysupgrade.openwrt.org}"
VERSION="${VERSION:-SNAPSHOT}"
PROFILE="${PROFILE:-tplink_deco-m5-v3}"
TARGET="ipq40xx/generic"
PACKAGES='"luci","hev-socks5-tproxy","kmod-nft-tproxy","kmod-nft-socket"'
OUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/firmware"

mkdir -p "$OUT_DIR"
req=$(printf '{"target":"%s","profile":"%s","version":"%s","packages":[%s],"client":"deco-openwrt/1.0"}' \
  "$TARGET" "$PROFILE" "$VERSION" "$PACKAGES")

echo "Requesting $VERSION build for $PROFILE ..."
hash=$(curl -fsS -m 60 -X POST -H 'Content-Type: application/json' -d "$req" "$ASU/api/v1/build" |
  python3 -I -c 'import json,sys; print(json.load(sys.stdin)["request_hash"])')

result="$OUT_DIR/asu-result.json"
for _ in $(seq 1 90); do
  code=$(curl -sS -m 30 -o "$result" -w '%{http_code}' "$ASU/api/v1/build/$hash")
  [ "$code" = "200" ] && break
  [ "$code" = "202" ] || { echo "ASU build failed (HTTP $code):"; tail -c 2000 "$result"; exit 1; }
  sleep 10
done
[ "$code" = "200" ] || { echo "ASU build timed out"; exit 1; }

python3 -I - "$result" <<'EOF' > "$OUT_DIR/images.txt"
import json, sys
d = json.load(open(sys.argv[1]))
for img in d["images"]:
    if img["type"] in ("factory", "sysupgrade"):
        print(img["type"], img["name"], img["sha256"], d["bin_dir"])
EOF

while read -r type name sha bin_dir; do
  echo "Downloading $type: $name"
  curl -fsS -m 300 -o "$OUT_DIR/$name" "$ASU/store/$bin_dir/$name"
  got=$(shasum -a 256 "$OUT_DIR/$name" | awk '{print $1}')
  [ "$got" = "$sha" ] || { echo "sha256 mismatch for $name"; exit 1; }
  ln -sf "$name" "$OUT_DIR/$PROFILE-$type.bin"
done < "$OUT_DIR/images.txt"

echo "OK. Images in $OUT_DIR:"
ls -l "$OUT_DIR"/*.bin
