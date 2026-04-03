"""
Internet Connection Sharing (ICS) Manager.
Handles enabling/disabling internet sharing between network adapters.

Methods (tried in order):
1. Windows ICS via COM (HNetCfg.HNetShare) — blocked by Group Policy on most corp machines
2. Registry override to temporarily bypass GP + ICS COM
3. Direct NAT via New-NetNat + IP forwarding — bypasses ICS entirely (like Connectify)
"""
import subprocess
import time
import atexit
import re

# NAT subnet used for sharing (avoid conflict with common subnets)
NAT_SUBNET = "192.168.137"
NAT_PREFIX = "192.168.137.0/24"
NAT_GATEWAY = "192.168.137.1"
NAT_NAME = "InternetShareNAT"


def _run_ps(command, timeout=30):
    """Run a PowerShell command and return (stdout, stderr, returncode)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "Command timed out", 1
    except Exception as e:
        return "", str(e), 1


# Track active sharing for cleanup
_active_sharing = {"source": None, "target": None, "method": None}


def _cleanup():
    """Cleanup handler to disable sharing on exit."""
    if _active_sharing["method"]:
        try:
            disable_sharing()
        except Exception:
            pass


atexit.register(_cleanup)


def _check_gp_blocks_ics():
    """Check if Group Policy blocks ICS."""
    stdout, _, _ = _run_ps(
        '(Get-ItemProperty "HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Network Connections" '
        '-Name NC_ShowSharedAccessUI -ErrorAction SilentlyContinue).NC_ShowSharedAccessUI'
    )
    # 0 = blocked, 1 = allowed, empty = not configured (allowed)
    return stdout.strip() == "0"


def _ensure_ics_service():
    """Ensure the ICS service is running."""
    stdout, _, _ = _run_ps("(Get-Service SharedAccess).Status")
    if stdout.lower() != "running":
        _run_ps("Start-Service SharedAccess", timeout=15)
        time.sleep(2)
        stdout, _, _ = _run_ps("(Get-Service SharedAccess).Status")
        if stdout.lower() != "running":
            return False, "Failed to start Internet Connection Sharing service"
    return True, "ICS service is running"


def disable_all_ics():
    """Disable all existing ICS connections (best-effort, may fail under GP)."""
    ps_script = """
    try {
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        foreach ($c in $connections) {
            try {
                $props = $m.NetConnectionProps($c)
                $config = $m.INetSharingConfigurationForINetConnection($c)
                if ($config.SharingEnabled) {
                    $config.DisableSharing()
                    Write-Output "Disabled sharing on: $($props.Name)"
                }
            } catch { }
        }
    } catch { }
    Write-Output "DONE"
    """
    stdout, stderr, rc = _run_ps(ps_script)
    return "DONE" in stdout, stdout


def enable_ics(source_name, target_name, log_callback=None):
    """
    Enable Internet Connection Sharing from source to target adapter.
    Tries multiple methods in order of preference.

    Returns:
        (success: bool, message: str)
    """
    def log(msg):
        if log_callback:
            log_callback(msg)

    log(f"Starting ICS: {source_name} → {target_name}")

    # Check Group Policy
    gp_blocks = _check_gp_blocks_ics()
    if gp_blocks:
        log("⚠ Group Policy blocks ICS (NC_ShowSharedAccessUI=0)")
        log("  Will skip COM methods and use direct NAT instead")
    else:
        # Step 1: Ensure ICS service
        log("Checking ICS service...")
        ok, msg = _ensure_ics_service()
        if not ok:
            log(f"  {msg}")
        else:
            log(f"  {msg}")

        # Step 2: Disable existing ICS
        log("Disabling any existing ICS connections...")
        ok, msg = disable_all_ics()
        log(f"  {msg}")
        time.sleep(1)

        # Step 3: Try ICS via COM
        log("Enabling ICS via COM interface...")
        success, message = _enable_ics_com(source_name, target_name, log)
        if success:
            # Verify it actually worked
            verified, details = verify_sharing(source_name, target_name)
            if verified:
                _active_sharing["source"] = source_name
                _active_sharing["target"] = target_name
                _active_sharing["method"] = "com"
                log("✓ ICS enabled and verified via COM!")
                return True, "Internet sharing is now active (ICS)"
            else:
                log(f"  COM reported success but verification failed: {details[:200]}")
                log("  ICS was not actually enabled (likely GP block)")

        # Step 4: Try registry override + COM
        log("Trying registry override to bypass Group Policy...")
        success, message = _enable_ics_registry_override(source_name, target_name, log)
        if success:
            verified, details = verify_sharing(source_name, target_name)
            if verified:
                _active_sharing["source"] = source_name
                _active_sharing["target"] = target_name
                _active_sharing["method"] = "registry"
                log("✓ ICS enabled via registry override!")
                return True, "Internet sharing is now active (registry override)"
            else:
                log(f"  Registry override also failed verification: {details[:200]}")

    # Step 5: Direct NAT (bypasses ICS entirely — like Connectify)
    log("=" * 50)
    log("Using direct NAT method (bypasses ICS/Group Policy)")
    log("=" * 50)
    success, message = _enable_nat_sharing(source_name, target_name, log)

    if success:
        _active_sharing["source"] = source_name
        _active_sharing["target"] = target_name
        _active_sharing["method"] = "nat"
        log("✓ Internet sharing active via direct NAT!")
        return True, "Internet sharing is now active (NAT method)"

    return False, f"All methods failed. Last error: {message}"


def _enable_ics_com(source_name, target_name, log):
    """Enable ICS using COM HNetCfg.HNetShare object."""
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")

    ps_script = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        $source = $null
        $target = $null

        foreach ($c in $connections) {{
            try {{
                $props = $m.NetConnectionProps($c)
                Write-Output "Found: $($props.Name) (GUID: $($props.Guid))"
                if ($props.Name -eq '{safe_source}') {{ $source = $c; Write-Output "  -> SOURCE" }}
                if ($props.Name -eq '{safe_target}') {{ $target = $c; Write-Output "  -> TARGET" }}
            }} catch {{ }}
        }}

        if (-not $source) {{ Write-Output "ERR: Source '{safe_source}' not found"; exit 1 }}
        if (-not $target) {{ Write-Output "ERR: Target '{safe_target}' not found"; exit 1 }}

        $sourceConfig = $m.INetSharingConfigurationForINetConnection($source)
        $sourceConfig.EnableSharing(0)  # PUBLIC
        Write-Output "Source set to PUBLIC"

        $targetConfig = $m.INetSharingConfigurationForINetConnection($target)
        $targetConfig.EnableSharing(1)  # PRIVATE
        Write-Output "Target set to PRIVATE"

        Write-Output "SUCCESS"
    }} catch {{
        Write-Output "COM_ERROR: $($_.Exception.Message)"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=30)
    log(f"  COM: {stdout[:400]}")

    if "SUCCESS" in stdout and "COM_ERROR" not in stdout:
        return True, "ICS enabled via COM"
    error_msg = ""
    for line in stdout.split("\n"):
        if "COM_ERROR:" in line or "ERR:" in line:
            error_msg = line
    return False, error_msg or "COM method failed"


def _enable_ics_registry_override(source_name, target_name, log):
    """Try to temporarily override GP registry keys and enable ICS."""
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")

    ps_script = f"""
    $regPath = 'HKLM:\\SOFTWARE\\Policies\\Microsoft\\Windows\\Network Connections'
    $originalValue = $null

    try {{
        # Save and override GP key
        $originalValue = (Get-ItemProperty $regPath -Name NC_ShowSharedAccessUI -ErrorAction SilentlyContinue).NC_ShowSharedAccessUI
        Set-ItemProperty $regPath -Name NC_ShowSharedAccessUI -Value 1 -Type DWord -Force
        Write-Output "GP override applied (was: $originalValue)"

        # Restart ICS service to pick up new policy
        Restart-Service SharedAccess -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3

        # Try COM
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        $source = $null; $target = $null

        foreach ($c in $connections) {{
            try {{
                $props = $m.NetConnectionProps($c)
                if ($props.Name -eq '{safe_source}') {{ $source = $c }}
                if ($props.Name -eq '{safe_target}') {{ $target = $c }}
            }} catch {{ }}
        }}

        if ($source -and $target) {{
            $sc = $m.INetSharingConfigurationForINetConnection($source)
            $sc.EnableSharing(0)
            $tc = $m.INetSharingConfigurationForINetConnection($target)
            $tc.EnableSharing(1)
            Write-Output "SUCCESS"
        }} else {{
            Write-Output "ERR: Adapters not found"
        }}
    }} catch {{
        Write-Output "REG_ERROR: $($_.Exception.Message)"
    }} finally {{
        # Restore original GP value
        if ($originalValue -ne $null) {{
            Set-ItemProperty $regPath -Name NC_ShowSharedAccessUI -Value $originalValue -Type DWord -Force
            Write-Output "GP restored to: $originalValue"
        }}
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=45)
    log(f"  Registry: {stdout[:400]}")

    if "SUCCESS" in stdout and "REG_ERROR" not in stdout:
        return True, "ICS enabled via registry override"
    return False, stdout


def _enable_nat_sharing(source_name, target_name, log):
    """
    Enable internet sharing using direct NAT (New-NetNat) + IP forwarding.
    This bypasses ICS entirely — similar to how Connectify works.

    Steps:
    1. Assign static IP to target adapter (acts as gateway)
    2. Enable IP forwarding in the registry
    3. Create NetNat for the target subnet
    4. Set up DNS forwarding via netsh
    """
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")

    # First clean up any previous NAT setup
    log("Cleaning up previous NAT configuration...")
    _cleanup_nat(target_name, log)
    time.sleep(1)

    # Step 1: Assign static IP to target adapter
    log(f"Assigning gateway IP {NAT_GATEWAY}/24 to {target_name}...")
    ps_step1 = f"""
    $ErrorActionPreference = 'Stop'
    try {{
        # Remove existing IPs on target
        Get-NetIPAddress -InterfaceAlias '{safe_target}' -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue

        # Remove existing routes
        Remove-NetRoute -InterfaceAlias '{safe_target}' -Confirm:$false -ErrorAction SilentlyContinue

        # Assign static IP
        New-NetIPAddress -InterfaceAlias '{safe_target}' -IPAddress '{NAT_GATEWAY}' -PrefixLength 24 -ErrorAction Stop | Out-Null
        Write-Output "SUCCESS"
    }} catch {{
        Write-Output "STEP1_ERROR: $($_.Exception.Message)"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_step1, timeout=20)
    log(f"  Static IP: {stdout}")
    if "STEP1_ERROR" in stdout:
        return False, f"Failed to assign static IP: {stdout}"

    # Step 2: Enable IP forwarding
    log("Enabling IP forwarding...")
    ps_step2 = """
    try {
        # Enable forwarding on all interfaces
        Set-NetIPInterface -Forwarding Enabled -ErrorAction SilentlyContinue
        # Also set registry key for persistence
        Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'IPEnableRouter' -Value 1 -Type DWord -Force
        Write-Output "SUCCESS"
    } catch {
        Write-Output "STEP2_ERROR: $($_.Exception.Message)"
    }
    """
    stdout, stderr, rc = _run_ps(ps_step2, timeout=15)
    log(f"  IP forwarding: {stdout}")
    if "STEP2_ERROR" in stdout:
        return False, f"Failed to enable IP forwarding: {stdout}"

    # Step 3: Create NetNat
    log(f"Creating NAT rule '{NAT_NAME}' for {NAT_PREFIX}...")
    ps_step3 = f"""
    try {{
        # Remove existing NAT with same name
        Remove-NetNat -Name '{NAT_NAME}' -Confirm:$false -ErrorAction SilentlyContinue

        # Create new NAT
        New-NetNat -Name '{NAT_NAME}' -InternalIPInterfaceAddressPrefix '{NAT_PREFIX}' -ErrorAction Stop | Out-Null
        Write-Output "SUCCESS"
    }} catch {{
        # If "overlaps" error, try removing all NetNat first
        if ($_.Exception.Message -like '*overlap*') {{
            Write-Output "Removing overlapping NAT..."
            Get-NetNat | Remove-NetNat -Confirm:$false -ErrorAction SilentlyContinue
            try {{
                New-NetNat -Name '{NAT_NAME}' -InternalIPInterfaceAddressPrefix '{NAT_PREFIX}' -ErrorAction Stop | Out-Null
                Write-Output "SUCCESS"
            }} catch {{
                Write-Output "STEP3_ERROR: $($_.Exception.Message)"
            }}
        }} else {{
            Write-Output "STEP3_ERROR: $($_.Exception.Message)"
        }}
    }}
    """
    stdout, stderr, rc = _run_ps(ps_step3, timeout=20)
    log(f"  NetNat: {stdout}")
    if "STEP3_ERROR" in stdout:
        return False, f"Failed to create NAT: {stdout}"

    # Step 4: Configure DNS forwarding on target adapter
    log("Configuring DNS on target adapter...")
    # Get DNS servers from source adapter
    ps_step4 = f"""
    try {{
        # Get DNS servers from source
        $dns = (Get-DnsClientServerAddress -InterfaceAlias '{safe_source}' -AddressFamily IPv4).ServerAddresses
        if (-not $dns -or $dns.Count -eq 0) {{
            $dns = @('8.8.8.8', '8.8.4.4')
            Write-Output "Using public DNS: $($dns -join ', ')"
        }} else {{
            Write-Output "Source DNS: $($dns -join ', ')"
        }}

        # Set DNS on target interface so clients can use it
        Set-DnsClientServerAddress -InterfaceAlias '{safe_target}' -ServerAddresses $dns -ErrorAction SilentlyContinue

        Write-Output "SUCCESS"
    }} catch {{
        Write-Output "STEP4_ERROR: $($_.Exception.Message)"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_step4, timeout=15)
    log(f"  DNS: {stdout}")

    # Step 5: Verify NAT is active
    log("Verifying NAT configuration...")
    ps_verify = f"""
    $nat = Get-NetNat -Name '{NAT_NAME}' -ErrorAction SilentlyContinue
    $ip = Get-NetIPAddress -InterfaceAlias '{safe_target}' -AddressFamily IPv4 -ErrorAction SilentlyContinue
    $fwd = (Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'IPEnableRouter').IPEnableRouter

    if ($nat -and $ip -and $fwd -eq 1) {{
        Write-Output "NAT: $($nat.Name) [$($nat.InternalIPInterfaceAddressPrefix)]"
        Write-Output "Gateway IP: $($ip.IPAddress)/$($ip.PrefixLength)"
        Write-Output "IP Forwarding: Enabled"
        Write-Output "VERIFIED"
    }} else {{
        Write-Output "NAT: $(if ($nat) {{ $nat.Name }} else {{ 'MISSING' }})"
        Write-Output "IP: $(if ($ip) {{ $ip.IPAddress }} else {{ 'MISSING' }})"
        Write-Output "Forwarding: $fwd"
        Write-Output "NOT_VERIFIED"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_verify, timeout=15)
    log(f"  Verify: {stdout}")

    if "VERIFIED" in stdout:
        log("")
        log("=" * 50)
        log("NAT sharing is active!")
        log(f"  Target adapter ({target_name}) gateway: {NAT_GATEWAY}")
        log(f"  Connected devices should use:")
        log(f"    IP: {NAT_SUBNET}.x (e.g., {NAT_SUBNET}.2)")
        log(f"    Subnet: 255.255.255.0")
        log(f"    Gateway: {NAT_GATEWAY}")
        log(f"    DNS: 8.8.8.8 / 8.8.4.4")
        log("=" * 50)
        return True, "NAT sharing active"

    return False, f"NAT verification failed: {stdout}"


def _cleanup_nat(target_name, log=None):
    """Remove NAT config and restore target adapter."""
    safe_target = target_name.replace("'", "''") if target_name else ""

    def _log(msg):
        if log:
            log(msg)

    ps_script = f"""
    # Remove NetNat
    Remove-NetNat -Name '{NAT_NAME}' -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed NAT rule"

    # Restore target adapter to DHCP
    if ('{safe_target}') {{
        Set-NetIPInterface -InterfaceAlias '{safe_target}' -Dhcp Enabled -ErrorAction SilentlyContinue
        Set-DnsClientServerAddress -InterfaceAlias '{safe_target}' -ResetServerAddresses -ErrorAction SilentlyContinue
        # Remove static IP
        Get-NetIPAddress -InterfaceAlias '{safe_target}' -IPAddress '{NAT_GATEWAY}' -ErrorAction SilentlyContinue |
            Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
        Write-Output "Restored {safe_target} to DHCP"
    }}

    # Disable IP forwarding (restore to default)
    Set-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'IPEnableRouter' -Value 0 -Type DWord -Force -ErrorAction SilentlyContinue
    Set-NetIPInterface -Forwarding Disabled -ErrorAction SilentlyContinue
    Write-Output "IP forwarding disabled"
    Write-Output "DONE"
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=20)
    _log(f"  Cleanup: {stdout}")
    return "DONE" in stdout


def disable_sharing():
    """Disable all active internet sharing."""
    method = _active_sharing.get("method")
    target = _active_sharing.get("target")

    results = []

    # Clean up ICS if it was used
    if method in ("com", "registry", None):
        ok, msg = disable_all_ics()
        results.append(f"ICS: {msg}")

    # Clean up NAT if it was used
    if method in ("nat", None):
        _cleanup_nat(target)
        results.append("NAT: cleaned up")

    _active_sharing["source"] = None
    _active_sharing["target"] = None
    _active_sharing["method"] = None

    return True, " | ".join(results)


def verify_sharing(source_name, target_name):
    """Verify that ICS is actually active between the specified adapters."""
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")

    ps_script = f"""
    $m = New-Object -ComObject HNetCfg.HNetShare
    $connections = $m.EnumEveryConnection
    $sourceSharing = $false
    $targetSharing = $false

    foreach ($c in $connections) {{
        try {{
            $props = $m.NetConnectionProps($c)
            $config = $m.INetSharingConfigurationForINetConnection($c)

            if ($props.Name -eq '{safe_source}' -and $config.SharingEnabled) {{
                $sourceSharing = $true
                Write-Output "Source '{safe_source}': ENABLED (Type=$($config.SharingConnectionType))"
            }}
            if ($props.Name -eq '{safe_target}' -and $config.SharingEnabled) {{
                $targetSharing = $true
                Write-Output "Target '{safe_target}': ENABLED (Type=$($config.SharingConnectionType))"
            }}
        }} catch {{ }}
    }}

    if ($sourceSharing -and $targetSharing) {{
        Write-Output "VERIFIED"
    }} else {{
        Write-Output "NOT_VERIFIED (src=$sourceSharing, tgt=$targetSharing)"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script)
    return "VERIFIED" in stdout, stdout


def get_sharing_status():
    """Get current sharing status (checks both ICS and NAT)."""
    parts = []

    # Check ICS
    ps_ics = """
    try {
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        $shared = @()
        foreach ($c in $connections) {
            try {
                $props = $m.NetConnectionProps($c)
                $config = $m.INetSharingConfigurationForINetConnection($c)
                if ($config.SharingEnabled) {
                    $type = if ($config.SharingConnectionType -eq 0) { "PUBLIC" } else { "PRIVATE" }
                    $shared += "$($props.Name)[$type]"
                }
            } catch {}
        }
        if ($shared.Count -gt 0) { Write-Output "ICS: $($shared -join ' | ')" }
        else { Write-Output "ICS: inactive" }
    } catch {
        Write-Output "ICS: error"
    }
    """
    stdout_ics, _, _ = _run_ps(ps_ics)
    parts.append(stdout_ics)

    # Check NAT
    ps_nat = f"""
    $nat = Get-NetNat -Name '{NAT_NAME}' -ErrorAction SilentlyContinue
    if ($nat) {{
        $fwd = (Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters' -Name 'IPEnableRouter' -ErrorAction SilentlyContinue).IPEnableRouter
        Write-Output "NAT: $($nat.Name) [$($nat.InternalIPInterfaceAddressPrefix)] Fwd=$fwd"
    }} else {{
        Write-Output "NAT: inactive"
    }}
    """
    stdout_nat, _, _ = _run_ps(ps_nat)
    parts.append(stdout_nat)

    return " | ".join(parts)


def enable_mobile_hotspot(ssid=None, password=None):
    """
    Enable Windows Mobile Hotspot programmatically.
    Uses the Windows.Networking.NetworkOperators API via PowerShell.
    """
    ps_script = """
    try {
        [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null

        $connectionProfile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()
        
        if ($connectionProfile -eq $null) {
            Write-Error "No internet connection found"
            exit 1
        }

        $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($connectionProfile)
        
        $capability = $manager.GetTetheringCapability()
        Write-Output "Tethering capability: $capability"
        
        if ($capability -ne 'Enabled') {
            Write-Error "Mobile Hotspot is not available: $capability"
            exit 1
        }
    """

    if ssid and password:
        safe_ssid = ssid.replace("'", "''")
        safe_password = password.replace("'", "''")
        ps_script += f"""
        # Set SSID and password
        $config = $manager.GetCurrentAccessPointConfiguration()
        $config.Ssid = '{safe_ssid}'
        $config.Passphrase = '{safe_password}'
        $asyncOp = $manager.ConfigureAccessPointAsync($config)
        # Wait for async op
        while ($asyncOp.Status -eq 'Started') {{ Start-Sleep -Milliseconds 100 }}
        """

    ps_script += """
        $state = $manager.TetheringOperationalState
        Write-Output "Current state: $state"
        
        if ($state -ne 'On') {
            $result = $manager.StartTetheringAsync().GetAwaiter().GetResult()
            Write-Output "Start result: $($result.Status)"
            if ($result.Status -ne 'Success') {
                Write-Error "Failed to start hotspot: $($result.Status)"
                exit 1
            }
        }
        
        Write-Output "SUCCESS"
    } catch {
        Write-Error "Mobile Hotspot error: $_"
        exit 1
    }
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=30)
    return "SUCCESS" in stdout, stdout + "\n" + stderr


def disable_mobile_hotspot():
    """Disable Windows Mobile Hotspot."""
    ps_script = """
    try {
        [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
        $connectionProfile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()
        
        if ($connectionProfile) {
            $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($connectionProfile)
            if ($manager.TetheringOperationalState -eq 'On') {
                $result = $manager.StopTetheringAsync().GetAwaiter().GetResult()
                Write-Output "Stop result: $($result.Status)"
            }
        }
        Write-Output "SUCCESS"
    } catch {
        Write-Error "Error: $_"
    }
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=20)
    return "SUCCESS" in stdout, stdout
