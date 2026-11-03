#!/usr/bin/env python3
"""Set Jellyfin's network, bitrate and GPU settings in its config files. Stop the container first.

Usage: configure_jellyfin.py <config dir> <docker subnet prefix, e.g. 172.30.96>
Rerun it, then start Jellyfin, after adding a device to the tailnet that should get full quality.
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HOME_LAN = "192.168.1.0/24"
# Keep one good stream inside the 5.7 Mbit/s home upload, measured 2026-11-01.
GUEST_BITRATE = 4_000_000
GPU_DECODES = ["h264", "hevc", "mpeg2video", "vc1", "vp8", "vp9"]  # RTX 2070: no AV1 decode.


def owner_devices():
    """Tailscale addresses of the owner's own devices. tailscale serve passes these on as the client's address."""
    status = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, check=True).stdout)
    self_owner = status["Self"]["UserID"]
    return [f"{ip}/{128 if ':' in ip else 32}" for p in status["Peer"].values() if p["UserID"] == self_owner for ip in p["TailscaleIPs"]]


def put(root, tag, value):
    """Set <tag> to a string, or to a list as child <string> elements, creating it if needed."""
    el = root.find(tag)
    if el is None:
        el = ET.SubElement(root, tag)
    if isinstance(value, list):
        el.clear()
        for item in value:
            ET.SubElement(el, "string").text = item
    else:
        el.text = value


def edit(path, values, new_root=None):
    tree = ET.parse(path) if path.exists() else ET.ElementTree(ET.Element(new_root))
    for tag, value in values.items():
        put(tree.getroot(), tag, value)
    ET.indent(tree)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main(config, prefix):
    gateway, share = f"{prefix}.1", f"{prefix}.3"
    edit(config / "network.xml", {
        # Trust the client address these two pass on: tailscale serve (arrives via the gateway) and movies-share.
        "KnownProxies": [gateway, share],
        # Only these count as "in network", so anyone else, meaning guests, gets the bitrate cap below.
        # The gateway itself is here for browsers on this PC using 127.0.0.1, which pass no client address.
        "LocalNetworkSubnets": [HOME_LAN, f"{gateway}/32", *owner_devices()],
        "EnableIPv6": "true",  # Phones often reach the tailnet over IPv6.
        "EnableRemoteAccess": "true",
    }, new_root="NetworkConfiguration")  # Jellyfin only writes this file once a setting changes.
    edit(config / "system.xml", {"RemoteClientBitrateLimit": str(GUEST_BITRATE)})
    edit(config / "encoding.xml", {
        "HardwareAccelerationType": "nvenc",
        "EnableHardwareEncoding": "true",
        "HardwareDecodingCodecs": GPU_DECODES,
        "EnableDecodingColorDepth10Hevc": "true",
        "EnableDecodingColorDepth10Vp9": "true",
        "EnableTonemapping": "true",
        "EnableThrottling": "true",
        "EnableSegmentDeletion": "true",
    })


if __name__ == "__main__":
    main(Path(sys.argv[1]) / "config", sys.argv[2])
