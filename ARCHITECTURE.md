# Internet Sharing Application — Architecture Plan

## 1. Problem Statement
- Corporate policy blocks native Windows Mobile Hotspot sharing
- Connectify works because it uses low-level Windows ICS/hosted network APIs
- Need: Python app to share internet from WiFi to Mobile Hotspot or Ethernet

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────┐
│                  UI Layer (tkinter)              │
│  ┌───────────┐ ┌──────────┐ ┌────────────────┐  │
│  │ Source     │ │ Target   │ │ Share/Stop     │  │
│  │ Dropdown  │ │ Dropdown │ │ Button         │  │
│  └───────────┘ └──────────┘ └────────────────┘  │
│  ┌──────────────────────────────────────────┐   │
│  │ Status Log / Console                     │   │
│  └──────────────────────────────────────────┘   │
└───────────────┬─────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────┐
│              Controller Layer                    │
│  - Validate adapter selection                    │
│  - Orchestrate enable/disable ICS                │
│  - Handle errors and rollback                    │
└───────────────┬─────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────┐
│              Network Engine Layer                │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ Adapter Discovery │  │ ICS Manager         │  │
│  │ (WMI/netsh)       │  │ (COM HNetCfg)       │  │
│  └──────────────────┘  └─────────────────────┘  │
│  ┌──────────────────┐  ┌─────────────────────┐  │
│  │ Hosted Network   │  │ Network Bridge      │  │
│  │ (netsh wlan)     │  │ (netsh bridge)      │  │
│  └──────────────────┘  └─────────────────────┘  │
└─────────────────────────────────────────────────┘
```

## 3. Key Components

### 3.1 Adapter Discovery (`network_utils.py`)
- List all network adapters using `Get-NetAdapter` / WMI
- Classify: WiFi, Ethernet, Virtual, Hotspot
- Show connection status (Connected/Disconnected)
- Show current IP, gateway info

### 3.2 ICS Manager (`ics_manager.py`)
- **Method 1: Windows ICS via COM (HNetCfg.HNetShare)**
  - Enable sharing on source adapter (public)
  - Enable sharing on target adapter (private)
  - Windows handles NAT, DHCP automatically
- **Method 2: Hosted Network + ICS**
  - Create hosted network via `netsh wlan set hostednetwork`
  - Then enable ICS from source → hosted network
- **Method 3: Network Bridge**
  - Bridge two adapters via `netsh bridge`
  - Fallback if ICS COM fails

### 3.3 UI Layer (`app.py`)
- tkinter window ~400x500px
- Source adapter dropdown (internet-connected adapters)
- Target adapter dropdown (available targets)
- Refresh button to re-scan adapters
- Share / Stop button
- Status log text area
- Admin privilege indicator

## 4. Corner Cases & Loopholes

### 4.1 Privilege Issues
- **MUST run as Administrator** — ICS requires elevated privileges
- App must detect if not admin and re-launch elevated (UAC prompt)

### 4.2 Adapter States
- WiFi adapter might disconnect/reconnect during sharing
- Target adapter might not have a cable plugged in (Ethernet)
- Virtual adapters (VPN, Hyper-V) should be filtered or labeled
- Adapter names can have Unicode characters

### 4.3 ICS Conflicts
- Only ONE ICS sharing session can be active at a time on Windows
- Must disable any existing ICS before enabling new one
- Windows Firewall service (SharedAccess) must be running
- ICS might fail silently — need to verify after enabling

### 4.4 Hosted Network Issues
- Not all WiFi adapters support hosted network
- Driver must support "Microsoft Hosted Network Virtual Adapter"
- If WiFi is already connected, hosted network uses same physical adapter
- Channel conflicts possible

### 4.5 Policy/GPO Blocks
- Group Policy can disable ICS (`NC_ShowSharedAccessUI`)
- Registry key override may be needed
- Some corporate environments block `netsh` commands
- Firewall rules may need modification

### 4.6 Network Bridge Gotchas
- Bridge removes IP from individual adapters
- Can cause brief network interruption during setup
- Bridge might not support all adapter types

### 4.7 Cleanup
- Must properly disable ICS on app close
- Hosted network must be stopped
- Bridge must be removed
- Failure to cleanup leaves system in bad state

## 5. Mitigation Strategy

| Risk | Mitigation |
|------|-----------|
| Not running as admin | Auto-elevate with UAC on startup |
| Existing ICS active | Disable all existing ICS before starting |
| SharedAccess service stopped | Auto-start the service |
| Adapter disappears | Monitor and auto-stop sharing |
| Silent ICS failure | Verify sharing status after enable |
| Cleanup on crash | Register atexit handler + signal handlers |
| Policy blocks ICS COM | Fallback to netsh commands |
| netsh also blocked | Try registry-based ICS enable |

## 6. Technology Stack
- **Python 3.8+**
- **tkinter** — built-in GUI
- **subprocess** — for netsh/PowerShell commands
- **comtypes/win32com** — for ICS COM interface (optional, fallback to netsh)
- **ctypes** — for admin check
- **wmi** (pip) — for adapter discovery (optional)

## 7. File Structure
```
share/
├── internet_share/
│   ├── __init__.py
│   ├── app.py              # Main UI application
│   ├── network_utils.py    # Adapter discovery & info
│   ├── ics_manager.py      # ICS enable/disable logic
│   └── admin_utils.py      # Admin privilege handling
├── test_sharing.py          # Test plan & tests
├── run.bat                  # Launcher with admin elevation
└── requirements.txt         # Dependencies (minimal)
```
