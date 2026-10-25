# Runbook: Deco M5 v3 → OpenWrt, mỗi SSID một proxy SOCKS5

## Thành phần

| File | Vai trò |
|---|---|
| `scripts/build-firmware.sh` | Gửi yêu cầu build image OpenWrt **SNAPSHOT chính thức** lên ASU (sysupgrade.openwrt.org) cho `tplink_deco-m5-v3`, nhúng sẵn `luci`, `hev-socks5-tproxy`, `kmod-nft-tproxy` và `kmod-nft-socket`, rồi tải image về `firmware/` và kiểm tra sha256. |
| `scripts/flash-tftp.sh` | (sudo) Đặt `en5` thành `192.168.0.66`, phục vụ `M5v1_tp_recovery.bin` qua tftpd của macOS và theo dõi request bằng tcpdump. `--stop` để tắt tftpd và đưa `en5` về DHCP. |
| `proxies.csv` | Danh sách SSID và proxy. Copy từ `proxies.example.csv`. File này bị git ignore vì chứa secret. |
| `scripts/generate-config.py` | Kiểm tra CSV và sinh `build/rootfs/` (hev yml, uci batch, nft rules, init script). |
| `scripts/deploy.sh` | Sinh config, đẩy lên router qua SSH, chạy `socks-ssid-apply`, rồi in trạng thái. `--check` chỉ in trạng thái. |
| `router/usr/sbin/socks-ssid-apply` | Chạy trên router. Xoá các section cũ do tool tạo, áp uci batch, reload các dịch vụ. Chạy lại nhiều lần vẫn cho cùng kết quả. |
| `router/etc/init.d/socks-ssid` | Dịch vụ procd chạy một tiến trình `hev-socks5-tproxy` cho mỗi file `/etc/socks-ssid/pxN.yml`. |
| `scripts/test-ssids-from-mac.sh` | Cho Wi-Fi của Mac join lần lượt từng SSID, so IP public đi qua SSID với IP khi Mac gọi thẳng proxy đó. Đồng thời kiểm tra IPv6 phải bị chặn. |

## Luồng dữ liệu

Proxy có `id` N trong CSV sẽ có:

- SSID trên `br-pxN` với subnet `172.20.N.0/24`. DHCP phát DNS công cộng, mặc định là `1.1.1.1 8.8.8.8`. RA và DHCPv6 bị tắt.
- Trong chain `socks_ssid_prerouting` (table `inet fw4`), mọi TCP/UDP từ `br-pxN` đi tới địa chỉ public bị TPROXY về cổng `12000+N` và gắn fwmark `0x100`. `ip rule fwmark 0x100 lookup 100` cùng `local 0.0.0.0/0 dev lo table 100` đưa các gói đó vào máy cục bộ.
- Tiến trình `hev-socks5-tproxy` thứ N nhận gói và chuyển tiếp qua upstream SOCKS5 thứ N. TCP dùng CONNECT, UDP dùng UDP ASSOCIATE. DNS là UDP/53 tới resolver công cộng nên cũng đi qua proxy.
- Zone firewall `pxN` đặt `forward REJECT` và `input REJECT`, chỉ mở DHCP và các gói đã được tproxy đánh dấu. Client trên SSID không vào được LAN, LuCI, SSH hay DNS của router. Khi hev chết, client mất mạng chứ không đi thẳng ra WAN (fail-closed).

## Quy trình

```sh
# 0. Image (đã build sẵn r37657; chạy lại khi muốn bản snapshot mới hơn)
scripts/build-firmware.sh

# 1. Flash qua TFTP
sudo scripts/flash-tftp.sh            # cắm cổng xa jack nguồn, giữ reset khi cắm điện
sudo scripts/flash-tftp.sh --stop     # khi LED ổn định

# 2. Truy cập lần đầu (SNAPSHOT: root chưa có mật khẩu)
#    DTS của OpenWrt có một cổng "lan" và một cổng "wan". Nếu sau khoảng 1 phút Mac
#    không nhận được IP 192.168.1.x, tức là cáp đang ở cổng WAN: chuyển sang cổng kia.
ssh root@192.168.1.1 passwd
ssh-copy-id root@192.168.1.1          # hoặc dán key vào LuCI > System > Administration
# Cắm cổng còn lại vào router nhà (WAN, DHCP).

# 3. Proxy
cp proxies.example.csv proxies.csv    # điền thông tin
scripts/deploy.sh                     # mặc định root@10.10.10.3 (WAN). Lần đầu chưa mở WAN: ROUTER=192.168.1.1

# 4. Kiểm tra
scripts/deploy.sh --check
scripts/test-ssids-from-mac.sh        # hoặc: scripts/test-ssids-from-mac.sh 1 3
```

Khi Mac chưa có IP `192.168.1.x` (ví dụ ngay sau khi flash), router vẫn truy cập được qua IPv6 link-local. Tìm địa chỉ bằng `ping6 -c2 ff02::1%en5` rồi chạy `ROUTER='fe80::…%en5' scripts/deploy.sh`.

Quản trị qua WAN: mặc định `deploy.sh` mở SSH và LuCI phía WAN (cổng 22, 80, 443) cho các dải `10.10.10.0/24 192.168.68.0/24 192.168.88.0/24`. SSH chỉ chấp nhận key, LuCI vẫn dùng mật khẩu root. Muốn đổi dải thì đặt `ADMIN_FROM="..."`, muốn tắt hẳn thì đặt `ADMIN_FROM=""`. Trên MikroTik, cổng WAN của Deco (MAC `E4:C3:2A:80:C2:69`) đã có lease tĩnh **`10.10.10.3`**, nên truy cập qua `ssh root@10.10.10.3` và `http://10.10.10.3`.

Muốn sửa proxy: sửa `proxies.csv` rồi chạy lại `scripts/deploy.sh`. Proxy nào bị xoá khỏi CSV thì SSID của nó cũng bị xoá trên router.

## Nâng cấp SNAPSHOT

Chạy `scripts/build-firmware.sh` để có image mới với kernel và kmod khớp nhau. Sau đó copy `firmware/tplink_deco-m5-v3-sysupgrade.bin` lên router và chạy `sysupgrade`. Các file của socks-ssid nằm trong `/etc/sysupgrade.conf` nên được giữ lại. Không chạy `apk add kmod-*` trên một snapshot cũ, vì kernel đã lệch so với feed hiện tại.

## Quay về firmware TP-Link

Tải firmware stock của Deco M5 v3 từ tp-link.com, rồi chạy `sudo scripts/flash-tftp.sh <stock.bin>` theo đúng thao tác ở bước 1.

## Gỡ lỗi

- `logread -e hev-socks5`: xem lỗi kết nối hoặc xác thực tới upstream.
- `nft list chain inet fw4 socks_ssid_prerouting`: phải có một rule tproxy cho mỗi proxy.
- `ip rule; ip route show table 100`: phải có rule fwmark và route local.
- Client có IP nhưng không ra được mạng: thử `curl -x socks5h://user:pass@host:port https://api.ipify.org` từ Mac để xem chính proxy có sống không.
- Nhiều SSID làm Wi-Fi chập chờn: giảm số SSID trên mỗi băng tần (cột `band`).
