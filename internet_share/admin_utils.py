"""
Admin privilege utilities for Windows.
Handles detection and elevation of administrator privileges.
"""
import ctypes
import sys
import os


def is_admin():
    """Check if the current process has administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def run_as_admin():
    """Re-launch the current script with administrator privileges via UAC."""
    if is_admin():
        return True

    script = os.path.abspath(sys.argv[0])
    params = " ".join([f'"{arg}"' for arg in sys.argv[1:]])

    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{script}" {params}', None, 1
        )
        # ShellExecuteW returns > 32 on success
        if ret > 32:
            sys.exit(0)
        else:
            return False
    except Exception:
        return False
