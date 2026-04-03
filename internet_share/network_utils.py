"""
Network adapter discovery and information utilities.
Uses PowerShell/netsh to enumerate adapters and their status.
"""
import subprocess
import json
import re


def _run_ps(command, timeout=15):
    """Run a PowerShell command and return stdout."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""


def get_all_adapters():
    """
    Get all network adapters with their details.
    Returns list of dicts with keys:
        name, description, status, mac, ifindex, media_type, ip_address, gateway
    """
    # Get adapter basic info
    ps_cmd = """
    Get-NetAdapter | Select-Object Name, InterfaceDescription, Status, 
        MacAddress, ifIndex, MediaType, 
        @{N='InterfaceType';E={$_.InterfaceType}} |
    ConvertTo-Json -Depth 2
    """
    raw = _run_ps(ps_cmd)
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # Ensure it's a list
    if isinstance(data, dict):
        data = [data]

    adapters = []
    for item in data:
        adapter = {
            "name": item.get("Name", ""),
            "description": item.get("InterfaceDescription", ""),
            "status": item.get("Status", "Unknown"),
            "mac": item.get("MacAddress", ""),
            "ifindex": item.get("ifIndex", 0),
            "media_type": item.get("MediaType", ""),
            "interface_type": item.get("InterfaceType", ""),
            "ip_address": "",
            "gateway": "",
        }
        adapters.append(adapter)

    # Get IP info for connected adapters
    ip_cmd = """
    Get-NetIPConfiguration | Where-Object { $_.IPv4Address } |
    Select-Object InterfaceAlias, 
        @{N='IP';E={($_.IPv4Address.IPAddress -join ',')}},
        @{N='Gateway';E={($_.IPv4DefaultGateway.NextHop -join ',')}} |
    ConvertTo-Json -Depth 2
    """
    ip_raw = _run_ps(ip_cmd)
    if ip_raw:
        try:
            ip_data = json.loads(ip_raw)
            if isinstance(ip_data, dict):
                ip_data = [ip_data]
            ip_map = {}
            for entry in ip_data:
                alias = entry.get("InterfaceAlias", "")
                ip_map[alias] = {
                    "ip": entry.get("IP", ""),
                    "gateway": entry.get("Gateway", ""),
                }
            for adapter in adapters:
                if adapter["name"] in ip_map:
                    adapter["ip_address"] = ip_map[adapter["name"]]["ip"]
                    adapter["gateway"] = ip_map[adapter["name"]]["gateway"]
        except json.JSONDecodeError:
            pass

    return adapters


def get_internet_adapters(adapters=None):
    """Get adapters that have an active internet connection (have a gateway)."""
    if adapters is None:
        adapters = get_all_adapters()
    return [a for a in adapters if a["status"] == "Up" and a["gateway"]]


def get_target_adapters(adapters=None, exclude_names=None):
    """
    Get adapters that can be used as sharing targets.
    Includes: Up adapters without gateway, disconnected Ethernet.
    Excludes: the source adapter, VMware/Hyper-V virtual adapters.
    """
    if adapters is None:
        adapters = get_all_adapters()
    if exclude_names is None:
        exclude_names = []

    # Filter out VMware, Hyper-V virtual switches, loopback
    virtual_keywords = ["vmware", "hyper-v", "virtual switch", "loopback"]

    targets = []
    for a in adapters:
        if a["name"] in exclude_names:
            continue
        desc_lower = a["description"].lower()
        if any(kw in desc_lower for kw in virtual_keywords):
            continue
        # Include: up adapters without gateway, or disconnected ethernet/bluetooth
        if a["status"] == "Up" and not a["gateway"]:
            targets.append(a)
        elif a["status"] in ("Disconnected", "Not Present"):
            name_lower = a["name"].lower()
            if "ethernet" in name_lower or "bluetooth" in name_lower:
                targets.append(a)
    return targets


def get_sharing_capable_targets(adapters=None, source_name=None):
    """
    Get all potential sharing targets: ethernet adapters and the
    'Local Area Connection* XX' adapter created by Mobile Hotspot.
    """
    if adapters is None:
        adapters = get_all_adapters()

    exclude = [source_name] if source_name else []
    targets = get_target_adapters(adapters, exclude)

    # Also look for Mobile Hotspot virtual adapter
    for a in adapters:
        if a["name"] in exclude:
            continue
        # Mobile Hotspot creates adapters like "Local Area Connection* 10"
        if re.match(r"Local Area Connection\*?\s*\d+", a["name"]):
            if a not in targets:
                targets.append(a)
        # Also match Wi-Fi Direct adapters
        if "wi-fi direct" in a["description"].lower():
            if a not in targets:
                targets.append(a)

    return targets


def get_adapter_display_name(adapter):
    """Format adapter info for display in dropdown."""
    status_icon = "●" if adapter["status"] == "Up" else "○"
    ip_info = f" [{adapter['ip_address']}]" if adapter["ip_address"] else ""
    gw_info = f" → {adapter['gateway']}" if adapter["gateway"] else ""
    return f"{status_icon} {adapter['name']} ({adapter['description']}){ip_info}{gw_info}"


def check_ics_service():
    """Check if the ICS (SharedAccess) service is running."""
    result = _run_ps("(Get-Service SharedAccess).Status")
    return result.strip().lower() == "running"


def start_ics_service():
    """Start the ICS service if not running."""
    _run_ps("Start-Service SharedAccess")
    return check_ics_service()
