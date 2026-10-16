"""Work out the URL guests on the local network should open, and render it as a QR code."""
import ipaddress
import io
import socket

import qrcode
import qrcode.image.svg

from .config import config


def lan_ips() -> list[str]:
    """This machine's private IPv4 addresses, the one used for outgoing traffic first.

    Looked up on every call, so the URL follows the machine to whatever network it's on.
    HOST_IP overrides detection (needed when the app can't see the real network, e.g. Docker
    with port mapping).
    """
    if config.host_ip:
        return [config.host_ip]
    ips: list[str] = []
    try:
        # Connecting a UDP socket sends nothing; it just picks the outgoing interface.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    seen, result = set(), []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if ip in seen or not addr.is_private or addr.is_loopback or addr.is_link_local:
            continue
        seen.add(ip)
        result.append(ip)
    # The outgoing interface is the real network. The other addresses are usually virtual
    # adapters (WSL, Hyper-V, VPN, Docker) that phones can't reach, so only fall back to them.
    return result[:1]


def share_info(port: int) -> dict:
    """Guest links.

    friendly: easiest to read/type (PUBLIC_URL, else the mDNS name like http://jukebox.local).
    qr:       what the QR code encodes; the IP address, since it works on every phone
              (PUBLIC_URL wins if set).
    """
    suffix = "" if port == 80 else f":{port}"
    ip_urls = [f"http://{ip}{suffix}/" for ip in lan_ips()]
    public = config.public_url.rstrip("/") + "/" if config.public_url else None
    mdns = f"http://{config.mdns_name}.local{suffix}/" if config.mdns_name else None
    friendly = public or mdns or (ip_urls[0] if ip_urls else None)
    qr = public or (ip_urls[0] if ip_urls else friendly)
    return {"friendly": friendly, "qr": qr, "ip": ip_urls[0] if ip_urls else None, "all_ips": ip_urls}


def qr_svg(data: str) -> bytes:
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()
