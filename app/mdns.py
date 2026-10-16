"""Advertise a friendly name like http://jukebox.local on the local network via mDNS.

iPhones, Macs, Windows 10+ and most recent Android phones resolve ".local" names; the
IP address stays available as a fallback (the QR code uses the IP for that reason).
"""
import asyncio
import logging
import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from .config import config
from .share import lan_ips

log = logging.getLogger("jukebox.mdns")

CHECK_INTERVAL = 30  # seconds between checks for a changed IP (laptop moved network)


def hostname() -> str | None:
    """The advertised host, e.g. 'jukebox.local', or None when disabled."""
    return f"{config.mdns_name}.local" if config.mdns_name else None


class MdnsAdvertiser:
    def __init__(self):
        self._zc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None
        self._ips: list[str] = []

    def _service(self, ips: list[str]) -> ServiceInfo:
        return ServiceInfo(
            "_http._tcp.local.",
            f"Jukebox ({config.mdns_name})._http._tcp.local.",
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=config.port,
            properties={"path": "/"},
            server=f"{hostname()}.",  # zeroconf also answers address lookups for this name
        )

    async def run(self) -> None:
        if not hostname():
            return
        try:
            while True:
                ips = lan_ips()
                if ips != self._ips:
                    await self._publish(ips)
                await asyncio.sleep(CHECK_INTERVAL)
        finally:
            await self.close()

    async def _publish(self, ips: list[str]) -> None:
        try:
            if self._zc is None:
                self._zc = AsyncZeroconf()
            if self._info is not None:
                await self._zc.async_unregister_service(self._info)
                self._info = None
            if ips:
                self._info = self._service(ips)
                await self._zc.async_register_service(self._info, allow_name_change=True)
                log.info("Advertising http://%s%s/ -> %s", hostname(), port_suffix(), ", ".join(ips))
            self._ips = ips
        except Exception:
            log.exception("mDNS advertising failed; the IP address still works")

    async def close(self) -> None:
        if self._zc is not None:
            try:
                if self._info is not None:
                    await self._zc.async_unregister_service(self._info)
                await self._zc.async_close()
            except Exception:
                pass
            self._zc = None


def port_suffix() -> str:
    return "" if config.port == 80 else f":{config.port}"


mdns = MdnsAdvertiser()
