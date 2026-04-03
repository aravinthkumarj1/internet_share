"""
System diagnostics for Internet Sharing.
Checks what sharing methods are available on the current machine.
"""
import subprocess


def _run_ps(command, timeout=15):
    """Run a PowerShell command and return stdout."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except Exception:
        return "", "", 1


def run_diagnostics():
    """
    Run full system diagnostics and return a dict of capabilities.
    This should be called once at startup (in a background thread).
    """
    diag = {
        "is_admin": False,
        "gp_blocks_ics": None,
        "gp_blocks_bridge": None,
        "ics_service": None,
        "winnat_driver": None,
        "winnat_can_create": None,
        "ip_forwarding": None,
        "rras_service": None,
        "hotspot_capable": None,
        "adapters": [],
        "methods_available": [],
        "recommendations": [],
    }

    # 1. Admin check
    out, _, _ = _run_ps(
        "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]"
        "::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    )
    diag["is_admin"] = out.strip().lower() == "true"

    # 2. Group Policy checks
    out, _, _ = _run_ps(
        'try { $v = (Get-ItemProperty "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Network Connections" '
        '-ErrorAction Stop); '
        '"ICS=" + $v.NC_ShowSharedAccessUI + ";BRIDGE=" + $v.NC_AllowNetBridge_NLA } '
        'catch { "NOKEY" }'
    )
    if "NOKEY" in out:
        diag["gp_blocks_ics"] = False
        diag["gp_blocks_bridge"] = False
    else:
        diag["gp_blocks_ics"] = "ICS=0" in out or "ICS=" in out and "ICS=1" not in out
        diag["gp_blocks_bridge"] = "BRIDGE=0" in out

    # 3. ICS service (SharedAccess)
    out, _, _ = _run_ps("(Get-Service SharedAccess -ErrorAction SilentlyContinue).Status")
    diag["ics_service"] = out.strip().lower() if out.strip() else "not_found"

    # 4. WinNat driver
    out, _, _ = _run_ps(
        "$svc = Get-Service WinNat -ErrorAction SilentlyContinue; "
        "if ($svc) { $svc.Status } else { 'not_found' }"
    )
    diag["winnat_driver"] = out.strip().lower() if out.strip() else "not_found"

    # 5. Test if New-NetNat actually works (only if admin)
    if diag["is_admin"]:
        out, err, rc = _run_ps(
            "try { New-NetNat -Name '_diag_test_' -InternalIPInterfaceAddressPrefix '10.253.253.0/24' "
            "-ErrorAction Stop | Out-Null; Remove-NetNat -Name '_diag_test_' -Confirm:$false "
            "-ErrorAction SilentlyContinue; 'WORKS' } "
            "catch { 'FAIL:' + $_.Exception.Message }",
            timeout=20
        )
        diag["winnat_can_create"] = "WORKS" in out
        if "FAIL:" in out:
            diag["winnat_error"] = out.split("FAIL:", 1)[1].strip()[:200]
    else:
        diag["winnat_can_create"] = None  # Unknown — need admin

    # 6. IP forwarding registry
    out, _, _ = _run_ps(
        "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' "
        "-Name 'IPEnableRouter' -ErrorAction SilentlyContinue).IPEnableRouter"
    )
    diag["ip_forwarding"] = out.strip() == "1"

    # 7. RRAS
    out, _, _ = _run_ps(
        "$svc = Get-Service RemoteAccess -ErrorAction SilentlyContinue; "
        "if ($svc) { $svc.Status + ',' + $svc.StartType } else { 'not_found' }"
    )
    diag["rras_service"] = out.strip().lower() if out.strip() else "not_found"

    # 8. Mobile Hotspot capability
    out, _, _ = _run_ps(
        "try { "
        "[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,"
        "Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null; "
        "$cp = [Windows.Networking.Connectivity.NetworkInformation,"
        "Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile(); "
        "if ($cp) { $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]"
        "::CreateFromConnectionProfile($cp); $mgr.GetTetheringCapability() } "
        "else { 'NoInternet' } } catch { 'Error:' + $_.Exception.Message }",
        timeout=15
    )
    cap = out.strip()
    diag["hotspot_capable"] = cap.lower() == "enabled" if cap else None
    diag["hotspot_status"] = cap

    # Determine available methods
    methods = []

    if not diag["gp_blocks_ics"] and diag["ics_service"] != "not_found":
        methods.append(("ICS (COM)", "Windows ICS via COM — standard method"))

    if diag["gp_blocks_ics"] and diag["is_admin"]:
        methods.append(("ICS (Registry Override)", "Temporarily override GP and enable ICS"))

    if diag["winnat_can_create"]:
        methods.append(("NAT (NetNat)", "Direct NAT via New-NetNat + IP forwarding"))

    if diag["is_admin"]:
        methods.append(("NAT (IP Forwarding)", "IP forwarding + static routing (no NAT kernel)"))

    # Python proxy always works as long as we have admin for the network config
    if diag["is_admin"]:
        methods.append(("Python Proxy", "SOCKS5/HTTP/DNS proxy — works when all NAT is blocked"))

    if diag["hotspot_capable"]:
        methods.append(("Mobile Hotspot", "Windows Mobile Hotspot via WinRT API"))

    diag["methods_available"] = methods

    # Recommendations
    recs = []
    if not diag["is_admin"]:
        recs.append("⚠ Run as Administrator for full functionality")
    if diag["gp_blocks_ics"]:
        recs.append("⚠ Group Policy blocks ICS — COM method will not work")
    if diag["gp_blocks_bridge"]:
        recs.append("⚠ Group Policy blocks Network Bridge")
    if diag["winnat_can_create"] is False:
        recs.append(f"⚠ New-NetNat unavailable: {diag.get('winnat_error', 'unknown error')}")
        recs.append("  → Will use IP forwarding + routing as fallback")
    if diag["hotspot_capable"]:
        recs.append("✓ Mobile Hotspot is available")
    elif diag.get("hotspot_status"):
        recs.append(f"⚠ Mobile Hotspot: {diag['hotspot_status']}")
    diag["recommendations"] = recs

    return diag


def format_diagnostics(diag):
    """Format diagnostics dict as human-readable text."""
    lines = [
        "System Diagnostics",
        "=" * 50,
        f"  Administrator:     {'Yes' if diag['is_admin'] else 'No'}",
        f"  GP Blocks ICS:     {'Yes' if diag['gp_blocks_ics'] else 'No'}",
        f"  GP Blocks Bridge:  {'Yes' if diag['gp_blocks_bridge'] else 'No'}",
        f"  ICS Service:       {diag['ics_service']}",
        f"  WinNat Driver:     {diag['winnat_driver']}",
        f"  WinNat Create:     {diag['winnat_can_create']}",
        f"  IP Forwarding:     {'Enabled' if diag['ip_forwarding'] else 'Disabled'}",
        f"  RRAS Service:      {diag['rras_service']}",
        f"  Mobile Hotspot:    {diag.get('hotspot_status', 'unknown')}",
        "",
        "Available Methods:",
    ]
    if diag["methods_available"]:
        for name, desc in diag["methods_available"]:
            lines.append(f"  ✓ {name}: {desc}")
    else:
        lines.append("  ✗ No sharing methods available!")

    if diag["recommendations"]:
        lines.append("")
        lines.append("Notes:")
        for r in diag["recommendations"]:
            lines.append(f"  {r}")

    lines.append("=" * 50)
    return "\n".join(lines)
