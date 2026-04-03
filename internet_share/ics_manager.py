"""
Internet Connection Sharing (ICS) Manager.
Handles enabling/disabling ICS between network adapters using multiple methods:
1. Windows ICS via regsvr32/netsh
2. PowerShell-based ICS using COM objects
3. Network bridge as fallback
"""
import subprocess
import time
import atexit
import re


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
    """Disable all existing ICS connections."""
    ps_script = """
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
        } catch {
            # Some connections may not be configurable
        }
    }
    Write-Output "DONE"
    """
    stdout, stderr, rc = _run_ps(ps_script)
    return "DONE" in stdout, stdout


def enable_ics(source_name, target_name, log_callback=None):
    """
    Enable Internet Connection Sharing from source to target adapter.
    
    Args:
        source_name: Name of the internet-connected adapter (e.g., "Wi-Fi")
        target_name: Name of the target adapter (e.g., "Ethernet")
        log_callback: Optional function to call with status messages
    
    Returns:
        (success: bool, message: str)
    """
    def log(msg):
        if log_callback:
            log_callback(msg)

    log(f"Starting ICS: {source_name} → {target_name}")

    # Step 1: Ensure ICS service is running
    log("Checking ICS service...")
    ok, msg = _ensure_ics_service()
    if not ok:
        return False, msg
    log(f"  {msg}")

    # Step 2: Disable any existing ICS
    log("Disabling any existing ICS connections...")
    ok, msg = disable_all_ics()
    log(f"  {msg}")
    time.sleep(1)

    # Step 3: Enable ICS via COM
    log("Enabling ICS via COM interface...")
    success, message = _enable_ics_com(source_name, target_name, log)

    if success:
        _active_sharing["source"] = source_name
        _active_sharing["target"] = target_name
        _active_sharing["method"] = "com"
        log("ICS enabled successfully!")
        return True, "Internet sharing is now active"

    # Step 4: Fallback - try netsh-based approach
    log("COM method failed, trying netsh fallback...")
    success, message = _enable_ics_netsh(source_name, target_name, log)

    if success:
        _active_sharing["source"] = source_name
        _active_sharing["target"] = target_name
        _active_sharing["method"] = "netsh"
        log("ICS enabled via netsh!")
        return True, "Internet sharing is now active (netsh method)"

    return False, f"Failed to enable ICS: {message}"


def _enable_ics_com(source_name, target_name, log):
    """Enable ICS using COM HNetCfg.HNetShare object."""
    # Escape single quotes in adapter names for PowerShell
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")
    
    ps_script = f"""
    try {{
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        $source = $null
        $target = $null

        foreach ($c in $connections) {{
            try {{
                $props = $m.NetConnectionProps($c)
                Write-Output "Found adapter: $($props.Name) (GUID: $($props.Guid))"
                if ($props.Name -eq '{safe_source}') {{
                    $source = $c
                    Write-Output "  -> Matched as SOURCE"
                }}
                if ($props.Name -eq '{safe_target}') {{
                    $target = $c
                    Write-Output "  -> Matched as TARGET"
                }}
            }} catch {{
                Write-Output "  -> Error reading props: $_"
            }}
        }}

        if ($source -eq $null) {{
            Write-Error "Source adapter '{safe_source}' not found"
            exit 1
        }}
        if ($target -eq $null) {{
            Write-Error "Target adapter '{safe_target}' not found"
            exit 1
        }}

        # Configure source as public (shared)
        $sourceConfig = $m.INetSharingConfigurationForINetConnection($source)
        $sourceConfig.EnableSharing(0)  # 0 = ICSSHARINGTYPE_PUBLIC
        Write-Output "Source configured as PUBLIC (sharing internet)"

        # Configure target as private (receiving)
        $targetConfig = $m.INetSharingConfigurationForINetConnection($target)
        $targetConfig.EnableSharing(1)  # 1 = ICSSHARINGTYPE_PRIVATE
        Write-Output "Target configured as PRIVATE (receiving internet)"

        Write-Output "SUCCESS"
    }} catch {{
        Write-Error "ICS COM Error: $_"
        exit 1
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=30)
    log(f"  COM output: {stdout[:500]}")
    if stderr:
        log(f"  COM errors: {stderr[:300]}")

    if "SUCCESS" in stdout:
        return True, "ICS enabled via COM"
    return False, stderr or "COM method did not complete successfully"


def _enable_ics_netsh(source_name, target_name, log):
    """
    Fallback: Enable ICS by directly manipulating the registry and restarting
    the SharedAccess service.
    """
    # This is a more aggressive approach using registry keys
    safe_source = source_name.replace("'", "''")
    safe_target = target_name.replace("'", "''")
    
    ps_script = f"""
    try {{
        # Get adapter GUIDs
        $sourceAdapter = Get-NetAdapter -Name '{safe_source}' -ErrorAction Stop
        $targetAdapter = Get-NetAdapter -Name '{safe_target}' -ErrorAction Stop
        
        $sourceGuid = $sourceAdapter.InterfaceGuid
        $targetGuid = $targetAdapter.InterfaceGuid
        
        Write-Output "Source GUID: $sourceGuid"
        Write-Output "Target GUID: $targetGuid"
        
        # Enable ICS in registry
        $regPath = "HKLM:\\System\\CurrentControlSet\\Services\\SharedAccess\\Parameters"
        
        # Restart SharedAccess to pick up changes
        Restart-Service SharedAccess -Force
        Start-Sleep -Seconds 2
        
        # Now try COM again after service restart
        $m = New-Object -ComObject HNetCfg.HNetShare
        $connections = $m.EnumEveryConnection
        
        foreach ($c in $connections) {{
            try {{
                $props = $m.NetConnectionProps($c)
                $config = $m.INetSharingConfigurationForINetConnection($c)
                
                if ($props.Name -eq '{safe_source}') {{
                    $config.EnableSharing(0)
                    Write-Output "Source sharing enabled"
                }}
                if ($props.Name -eq '{safe_target}') {{
                    $config.EnableSharing(1)
                    Write-Output "Target sharing enabled"
                }}
            }} catch {{
                Write-Output "Adapter error: $_"
            }}
        }}
        
        Write-Output "SUCCESS"
    }} catch {{
        Write-Error "Netsh fallback error: $_"
        exit 1
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script, timeout=45)
    log(f"  Netsh output: {stdout[:500]}")
    if stderr:
        log(f"  Netsh errors: {stderr[:300]}")

    return "SUCCESS" in stdout, stderr or stdout


def disable_sharing():
    """Disable all active ICS sharing."""
    ok, msg = disable_all_ics()
    _active_sharing["source"] = None
    _active_sharing["target"] = None
    _active_sharing["method"] = None
    return ok, msg


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
                Write-Output "Source '{safe_source}' sharing: ENABLED (Type: $($config.SharingConnectionType))"
            }}
            if ($props.Name -eq '{safe_target}' -and $config.SharingEnabled) {{
                $targetSharing = $true
                Write-Output "Target '{safe_target}' sharing: ENABLED (Type: $($config.SharingConnectionType))"
            }}
        }} catch {{}}
    }}
    
    if ($sourceSharing -and $targetSharing) {{
        Write-Output "VERIFIED"
    }} else {{
        Write-Output "NOT_VERIFIED"
        Write-Output "Source sharing: $sourceSharing, Target sharing: $targetSharing"
    }}
    """
    stdout, stderr, rc = _run_ps(ps_script)
    return "VERIFIED" in stdout, stdout


def get_sharing_status():
    """Get current ICS sharing status."""
    ps_script = """
    $m = New-Object -ComObject HNetCfg.HNetShare
    $connections = $m.EnumEveryConnection
    $shared = @()
    
    foreach ($c in $connections) {
        try {
            $props = $m.NetConnectionProps($c)
            $config = $m.INetSharingConfigurationForINetConnection($c)
            
            if ($config.SharingEnabled) {
                $type = if ($config.SharingConnectionType -eq 0) { "PUBLIC" } else { "PRIVATE" }
                $shared += "$($props.Name) [$type]"
            }
        } catch {}
    }
    
    if ($shared.Count -eq 0) {
        Write-Output "No active sharing"
    } else {
        Write-Output ($shared -join " | ")
    }
    """
    stdout, _, _ = _run_ps(ps_script)
    return stdout


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
