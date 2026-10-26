#!/usr/bin/env python3
"""Generate the router-side files for "one SSID = one SOCKS5 proxy".

Reads proxies.csv and writes build/rootfs/, a tree that deploy.sh unpacks
onto the router at /:

  etc/socks-ssid/pxN.yml          hev-socks5-tproxy config per proxy (0600)
  etc/socks-ssid/socks-ssid.uci   uci batch: network/wireless/dhcp/firewall (0600)
  etc/nftables.d/90-socks-ssid.nft  TPROXY rules, included by fw4
  etc/init.d/socks-ssid           procd service running every pxN.yml

Each proxy N gets bridge br-pxN, subnet 172.20.N.0/24, its own firewall zone
with no forwarding (fail-closed) and TPROXY port BASE_PORT+N.

Usage: generate-config.py [--csv proxies.csv] [--out build] [--country VN]
"""
import argparse
import csv
import ipaddress
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIELDS = ["id", "ssid", "wifi_key", "band", "proxy_host", "proxy_port",
          "proxy_user", "proxy_pass", "dns"]
BANDS = {"2g": ["2g"], "5g": ["5g"], "both": ["2g", "5g"]}
FWMARK = "0x100"
ROUTE_TABLE = "100"
BASE_PORT = 12000
# Client subnet of proxy N is CLIENT_NET.N.0/24 (inside 172.20.0.0/16).
CLIENT_NET = "172.20"
# ath10k on QCA4019 is reliable up to about 8 APs per radio; 16 is the
# firmware's hard ceiling.
VAP_WARN, VAP_MAX = 8, 16
BYPASS4 = ["0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
           "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
           "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4"]


def fail(msg):
    sys.exit(f"error: {msg}")


def uci_quote(value):
    return "'" + str(value).replace("'", "'\\''") + "'"


def yaml_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def load_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(
            line for line in fh if line.strip() and not line.lstrip().startswith("#"))
        missing = [f for f in FIELDS if f not in (reader.fieldnames or [])]
        if missing:
            fail(f"{path}: missing columns {', '.join(missing)}")
        rows = [{k: (v or "").strip() for k, v in r.items()} for r in reader]
    if not rows:
        fail(f"{path}: no proxies defined")

    seen_ids, seen_ssids = set(), set()
    radio_load = {"2g": 0, "5g": 0}
    for n, r in enumerate(rows, start=1):
        where = f"{path} row {n} (ssid {r.get('ssid')!r})"
        try:
            r["id"] = int(r["id"])
            r["proxy_port"] = int(r["proxy_port"])
        except ValueError:
            fail(f"{where}: id and proxy_port must be integers")
        if not 1 <= r["id"] <= 250:
            fail(f"{where}: id must be 1..250")
        if r["id"] in seen_ids:
            fail(f"{where}: duplicate id {r['id']}")
        if not 1 <= len(r["ssid"].encode()) <= 32:
            fail(f"{where}: ssid must be 1..32 bytes")
        if r["ssid"] in seen_ssids:
            fail(f"{where}: duplicate ssid {r['ssid']!r}")
        if not 8 <= len(r["wifi_key"]) <= 63:
            fail(f"{where}: wifi_key must be 8..63 characters")
        if r["band"] not in BANDS:
            fail(f"{where}: band must be one of {', '.join(BANDS)}")
        if not r["proxy_host"]:
            fail(f"{where}: proxy_host is empty")
        if not 1 <= r["proxy_port"] <= 65535:
            fail(f"{where}: proxy_port out of range")
        if bool(r["proxy_user"]) != bool(r["proxy_pass"]):
            fail(f"{where}: set both proxy_user and proxy_pass, or neither")
        dns = [d.strip() for d in (r["dns"] or "1.1.1.1 8.8.8.8").split() if d.strip()]
        for d in dns:
            if not is_ip(d) or ipaddress.ip_address(d).version != 4:
                fail(f"{where}: dns must be space separated IPv4 addresses")
            if any(ipaddress.ip_address(d) in ipaddress.ip_network(b) for b in BYPASS4):
                fail(f"{where}: dns {d} is private and would bypass the proxy")
        r["dns"] = dns
        if is_ip(r["proxy_host"]) and \
                ipaddress.ip_address(r["proxy_host"]) in ipaddress.ip_network(f"{CLIENT_NET}.0.0/16"):
            fail(f"{where}: proxy_host is inside the client range {CLIENT_NET}.0.0/16; "
                 "change CLIENT_NET in generate-config.py")
        if not is_ip(r["proxy_host"]):
            print(f"warning: {where}: proxy_host {r['proxy_host']!r} is a hostname; "
                  "check `logread -e socks-ssid` after deploy, use an IP if it fails",
                  file=sys.stderr)
        for band in BANDS[r["band"]]:
            radio_load[band] += 1
        seen_ids.add(r["id"])
        seen_ssids.add(r["ssid"])

    for band, count in radio_load.items():
        if count > VAP_MAX:
            fail(f"{count} SSIDs on {band}: the radio supports at most {VAP_MAX}")
        if count > VAP_WARN:
            print(f"warning: {count} SSIDs on {band}; beyond {VAP_WARN} expect "
                  "airtime loss or driver instability", file=sys.stderr)
    return sorted(rows, key=lambda r: r["id"])


def hev_yaml(r):
    port = BASE_PORT + r["id"]
    lines = [
        f"# {r['ssid']} -> {r['proxy_host']}:{r['proxy_port']}",
        "main:",
        "  workers: 1",
        "socks5:",
        f"  port: {r['proxy_port']}",
        f"  address: {yaml_quote(r['proxy_host'])}",
        "  udp: 'udp'",
    ]
    if r["proxy_user"]:
        lines += [f"  username: {yaml_quote(r['proxy_user'])}",
                  f"  password: {yaml_quote(r['proxy_pass'])}"]
    lines += [
        "tcp:",
        f"  port: {port}",
        "  address: '0.0.0.0'",
        "udp:",
        f"  port: {port}",
        "  address: '0.0.0.0'",
        "misc:",
        "  log-file: stderr",
        "  log-level: warn",
        "  limit-nofile: 65535",
        # Every UDP flow (each DNS query) holds its own SOCKS5 control connection
        # until this idle timeout. The default 60s piles up ~100 of them per busy
        # client, which exhausts 3proxy's per-listener connection limit.
        "  udp-read-write-timeout: 15000",
    ]
    return "\n".join(lines) + "\n"


def nft_rules(rows):
    ifaces = ", ".join(f'"br-px{r["id"]}"' for r in rows)
    out = [
        "# Generated by scripts/generate-config.py. Included by fw4 into table inet fw4.",
        "set socks_ssid_bypass4 {",
        "\ttype ipv4_addr",
        "\tflags interval",
        f"\telements = {{ {', '.join(BYPASS4)} }}",
        "}",
        "",
        "chain socks_ssid_prerouting {",
        "\ttype filter hook prerouting priority mangle; policy accept;",
        f"\tiifname != {{ {ifaces} }} return",
        "\tmeta nfproto != ipv4 return",
        "\tip daddr @socks_ssid_bypass4 return",
        f"\tmeta l4proto tcp socket transparent 1 meta mark set meta mark | {FWMARK} accept",
    ]
    for r in rows:
        out.append(f'\tiifname "br-px{r["id"]}" meta l4proto {{ tcp, udp }} '
                   f'tproxy ip to :{BASE_PORT + r["id"]} '
                   f"meta mark set meta mark | {FWMARK} accept")
    out.append("}")
    return "\n".join(out) + "\n"


def uci_batch(rows, country, admin_from):
    out = []

    def s(key, value):
        out.append(f"set {key}={uci_quote(value)}")

    def add(key, value):
        out.append(f"add_list {key}={uci_quote(value)}")

    if admin_from:
        # SSH (key only) and LuCI reachable from the upstream LAN via WAN.
        s("firewall.socks_ssid_wan_admin", "rule")
        s("firewall.socks_ssid_wan_admin.name", "wan-admin")
        s("firewall.socks_ssid_wan_admin.src", "wan")
        s("firewall.socks_ssid_wan_admin.proto", "tcp")
        s("firewall.socks_ssid_wan_admin.dest_port", "22 80 443")
        s("firewall.socks_ssid_wan_admin.family", "ipv4")
        s("firewall.socks_ssid_wan_admin.target", "ACCEPT")
        for net in admin_from:
            add("firewall.socks_ssid_wan_admin.src_ip", net)
        s("dropbear.@dropbear[0].PasswordAuth", "off")
        s("dropbear.@dropbear[0].RootPasswordAuth", "off")

    for radio in ("@RADIO_2G@", "@RADIO_5G@"):
        s(f"wireless.{radio}.disabled", "0")
        s(f"wireless.{radio}.country", country)

    s("network.socks_ssid_rule", "rule")
    s("network.socks_ssid_rule.mark", f"{FWMARK}/{FWMARK}")
    s("network.socks_ssid_rule.lookup", ROUTE_TABLE)
    s("network.socks_ssid_route", "route")
    s("network.socks_ssid_route.interface", "loopback")
    s("network.socks_ssid_route.type", "local")
    s("network.socks_ssid_route.target", "0.0.0.0/0")
    s("network.socks_ssid_route.table", ROUTE_TABLE)

    for r in rows:
        n = r["id"]
        px = f"px{n}"
        s(f"network.br_{px}", "device")
        s(f"network.br_{px}.name", f"br-{px}")
        s(f"network.br_{px}.type", "bridge")
        s(f"network.br_{px}.bridge_empty", "1")
        s(f"network.{px}", "interface")
        s(f"network.{px}.device", f"br-{px}")
        s(f"network.{px}.proto", "static")
        s(f"network.{px}.ipaddr", f"{CLIENT_NET}.{n}.1/24")

        for band in BANDS[r["band"]]:
            w = f"wireless.{px}_{band}"
            s(w, "wifi-iface")
            s(f"{w}.device", "@RADIO_2G@" if band == "2g" else "@RADIO_5G@")
            s(f"{w}.mode", "ap")
            s(f"{w}.network", px)
            s(f"{w}.ssid", r["ssid"])
            s(f"{w}.encryption", "sae-mixed")
            s(f"{w}.key", r["wifi_key"])

        s(f"dhcp.{px}", "dhcp")
        s(f"dhcp.{px}.interface", px)
        s(f"dhcp.{px}.start", "100")
        s(f"dhcp.{px}.limit", "150")
        s(f"dhcp.{px}.leasetime", "12h")
        # Current dnsmasq init defaults dhcpv4 to disabled.
        s(f"dhcp.{px}.dhcpv4", "server")
        s(f"dhcp.{px}.ra", "disabled")
        s(f"dhcp.{px}.dhcpv6", "disabled")
        add(f"dhcp.{px}.dhcp_option", "6," + ",".join(r["dns"]))

        s(f"firewall.{px}", "zone")
        s(f"firewall.{px}.name", px)
        add(f"firewall.{px}.network", px)
        s(f"firewall.{px}.input", "REJECT")
        s(f"firewall.{px}.output", "ACCEPT")
        s(f"firewall.{px}.forward", "REJECT")
        s(f"firewall.{px}_dhcp", "rule")
        s(f"firewall.{px}_dhcp.name", f"{px}-dhcp")
        s(f"firewall.{px}_dhcp.src", px)
        s(f"firewall.{px}_dhcp.proto", "udp")
        s(f"firewall.{px}_dhcp.dest_port", "67")
        s(f"firewall.{px}_dhcp.family", "ipv4")
        s(f"firewall.{px}_dhcp.target", "ACCEPT")
        s(f"firewall.{px}_tproxy", "rule")
        s(f"firewall.{px}_tproxy.name", f"{px}-tproxy")
        s(f"firewall.{px}_tproxy.src", px)
        s(f"firewall.{px}_tproxy.proto", "tcp udp")
        s(f"firewall.{px}_tproxy.mark", f"{FWMARK}/{FWMARK}")
        s(f"firewall.{px}_tproxy.family", "ipv4")
        s(f"firewall.{px}_tproxy.target", "ACCEPT")
    return "\n".join(out) + "\n"


def write(path, content, mode):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.chmod(path, mode)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=os.path.join(ROOT, "proxies.csv"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build"))
    ap.add_argument("--country", default="VN")
    ap.add_argument("--admin-from", default="",
                    help="space separated IPv4 networks allowed to reach SSH/LuCI via WAN")
    args = ap.parse_args()

    admin_from = args.admin_from.split()
    for net in admin_from:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            fail(f"--admin-from: {net!r} is not an IPv4 network")
    rows = load_rows(args.csv)
    rootfs = os.path.join(args.out, "rootfs")
    shutil.rmtree(rootfs, ignore_errors=True)

    for r in rows:
        write(os.path.join(rootfs, f"etc/socks-ssid/px{r['id']}.yml"), hev_yaml(r), 0o600)
    write(os.path.join(rootfs, "etc/socks-ssid/socks-ssid.uci"),
          uci_batch(rows, args.country, admin_from), 0o600)
    write(os.path.join(rootfs, "etc/nftables.d/90-socks-ssid.nft"), nft_rules(rows), 0o644)
    init_src = os.path.join(ROOT, "router/etc/init.d/socks-ssid")
    init_dst = os.path.join(rootfs, "etc/init.d/socks-ssid")
    os.makedirs(os.path.dirname(init_dst), exist_ok=True)
    shutil.copyfile(init_src, init_dst)
    os.chmod(init_dst, 0o755)

    for r in rows:
        print(f"px{r['id']:<4} {r['ssid']:<32} {r['band']:<4} {CLIENT_NET}.{r['id']}.0/24 "
              f"tproxy:{BASE_PORT + r['id']} -> {r['proxy_host']}:{r['proxy_port']}")
    print(f"Wrote {rootfs}")


if __name__ == "__main__":
    main()
