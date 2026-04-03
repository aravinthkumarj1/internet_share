"""
Internet Sharing Application — Main UI
Minimal tkinter interface to share internet between network adapters.
"""
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import threading
import sys
import os

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internet_share.admin_utils import is_admin, run_as_admin
from internet_share.network_utils import (
    get_all_adapters, get_internet_adapters, get_sharing_capable_targets,
    get_adapter_display_name, check_ics_service, start_ics_service,
)
from internet_share.ics_manager import (
    enable_ics, disable_sharing, verify_sharing, get_sharing_status,
    enable_mobile_hotspot, disable_mobile_hotspot,
)


class InternetShareApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Internet Share")
        self.root.geometry("620x520")
        self.root.resizable(True, True)
        self.root.minsize(500, 400)

        # State
        self.adapters = []
        self.source_adapters = []
        self.target_adapters = []
        self.is_sharing = False
        self.current_source = None
        self.current_target = None

        self._build_ui()
        self._check_admin()
        self._refresh_adapters()

        # Handle window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Style
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Share.TButton", font=("Segoe UI", 10, "bold"))

        # Main frame with padding
        main = ttk.Frame(self.root, padding=15)
        main.pack(fill=tk.BOTH, expand=True)

        # Title row
        title_frame = ttk.Frame(main)
        title_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(title_frame, text="Internet Share", style="Title.TLabel").pack(side=tk.LEFT)
        self.admin_label = ttk.Label(title_frame, text="", style="Status.TLabel")
        self.admin_label.pack(side=tk.RIGHT)

        # Source adapter
        source_frame = ttk.LabelFrame(main, text="Internet Source (connected adapter)", padding=8)
        source_frame.pack(fill=tk.X, pady=(0, 8))

        self.source_var = tk.StringVar()
        self.source_combo = ttk.Combobox(source_frame, textvariable=self.source_var,
                                          state="readonly", width=70)
        self.source_combo.pack(fill=tk.X)
        self.source_combo.bind("<<ComboboxSelected>>", self._on_source_changed)

        # Target adapter
        target_frame = ttk.LabelFrame(main, text="Share To (target adapter)", padding=8)
        target_frame.pack(fill=tk.X, pady=(0, 8))

        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(target_frame, textvariable=self.target_var,
                                          state="readonly", width=70)
        self.target_combo.pack(fill=tk.X)

        # Mobile Hotspot option
        hotspot_frame = ttk.Frame(main)
        hotspot_frame.pack(fill=tk.X, pady=(0, 8))
        self.hotspot_var = tk.BooleanVar(value=False)
        self.hotspot_check = ttk.Checkbutton(
            hotspot_frame, text="Enable Mobile Hotspot (creates WiFi hotspot for other devices)",
            variable=self.hotspot_var
        )
        self.hotspot_check.pack(side=tk.LEFT)

        # Button row
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill=tk.X, pady=(0, 8))

        self.share_btn = ttk.Button(btn_frame, text="▶ Start Sharing",
                                     command=self._toggle_sharing, style="Share.TButton")
        self.share_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.refresh_btn = ttk.Button(btn_frame, text="↻ Refresh Adapters",
                                       command=self._refresh_adapters_threaded)
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.status_btn = ttk.Button(btn_frame, text="ℹ Check Status",
                                      command=self._check_status)
        self.status_btn.pack(side=tk.LEFT)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(main, textvariable=self.status_var, style="Status.TLabel",
                                relief=tk.SUNKEN, padding=4)
        status_bar.pack(fill=tk.X, pady=(0, 8))

        # Log area
        log_frame = ttk.LabelFrame(main, text="Log", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=10, font=("Consolas", 9),
                                                   state=tk.DISABLED, wrap=tk.WORD)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def _log(self, message):
        """Add message to log area (thread-safe)."""
        def _do():
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.root.after(0, _do)

    def _set_status(self, text):
        """Update status bar (thread-safe)."""
        self.root.after(0, lambda: self.status_var.set(text))

    def _check_admin(self):
        if is_admin():
            self.admin_label.config(text="✓ Admin", foreground="green")
            self._log("Running with administrator privileges")
        else:
            self.admin_label.config(text="✗ Not Admin", foreground="red")
            self._log("WARNING: Not running as administrator!")
            self._log("ICS requires admin privileges. Attempting elevation...")
            result = messagebox.askyesno(
                "Administrator Required",
                "Internet sharing requires administrator privileges.\n\n"
                "Restart with elevated privileges?",
                icon="warning"
            )
            if result:
                run_as_admin()
                # If we get here, elevation failed
                self._log("Failed to elevate. Some features may not work.")
            else:
                self._log("Continuing without admin — sharing will likely fail.")

    def _refresh_adapters(self):
        """Refresh adapter lists."""
        self._log("Scanning network adapters...")
        self._set_status("Scanning adapters...")

        self.adapters = get_all_adapters()
        self.source_adapters = get_internet_adapters(self.adapters)

        # Populate source dropdown
        source_names = [get_adapter_display_name(a) for a in self.source_adapters]
        self.source_combo["values"] = source_names

        if source_names:
            self.source_combo.current(0)
            self._on_source_changed(None)
        else:
            self._log("No internet-connected adapters found!")
            self.source_combo["values"] = ["No internet adapters found"]

        self._log(f"Found {len(self.adapters)} adapters, {len(self.source_adapters)} with internet")
        for a in self.adapters:
            status_icon = "✓" if a["status"] == "Up" else "✗"
            gw = f" GW:{a['gateway']}" if a["gateway"] else ""
            self._log(f"  {status_icon} {a['name']} - {a['description']}{gw}")

        self._set_status("Ready")

    def _refresh_adapters_threaded(self):
        """Refresh adapters in background thread."""
        self.refresh_btn.config(state=tk.DISABLED)
        def _do():
            self._refresh_adapters()
            self.root.after(0, lambda: self.refresh_btn.config(state=tk.NORMAL))
        threading.Thread(target=_do, daemon=True).start()

    def _on_source_changed(self, event):
        """Update target list when source changes."""
        idx = self.source_combo.current()
        if idx < 0 or idx >= len(self.source_adapters):
            return

        source = self.source_adapters[idx]
        self.target_adapters = get_sharing_capable_targets(self.adapters, source["name"])

        target_names = [get_adapter_display_name(a) for a in self.target_adapters]

        # Always add Ethernet option even if disconnected
        has_ethernet = any("ethernet" in a["name"].lower() for a in self.target_adapters)
        if not has_ethernet:
            for a in self.adapters:
                if "ethernet" in a["name"].lower() and a["name"] != source["name"]:
                    self.target_adapters.append(a)
                    target_names.append(get_adapter_display_name(a) + " [cable needed]")

        self.target_combo["values"] = target_names
        if target_names:
            self.target_combo.current(0)
        else:
            self.target_combo["values"] = ["No target adapters available"]

    def _toggle_sharing(self):
        """Start or stop sharing."""
        if self.is_sharing:
            self._stop_sharing()
        else:
            self._start_sharing()

    def _start_sharing(self):
        """Start internet sharing in background thread."""
        src_idx = self.source_combo.current()
        tgt_idx = self.target_combo.current()

        if src_idx < 0 or src_idx >= len(self.source_adapters):
            messagebox.showerror("Error", "Please select a source adapter")
            return
        if tgt_idx < 0 or tgt_idx >= len(self.target_adapters):
            messagebox.showerror("Error", "Please select a target adapter")
            return

        source = self.source_adapters[src_idx]
        target = self.target_adapters[tgt_idx]

        if source["name"] == target["name"]:
            messagebox.showerror("Error", "Source and target cannot be the same adapter")
            return

        self.share_btn.config(state=tk.DISABLED)
        self._set_status("Starting sharing...")

        def _do():
            try:
                # Enable mobile hotspot if checked
                if self.hotspot_var.get():
                    self._log("Enabling Mobile Hotspot...")
                    ok, msg = enable_mobile_hotspot()
                    self._log(f"  Hotspot: {msg[:200]}")
                    if not ok:
                        self._log("WARNING: Mobile Hotspot activation failed")
                        self._log("Continuing with ICS setup...")

                # Enable ICS
                ok, msg = enable_ics(source["name"], target["name"], log_callback=self._log)

                if ok:
                    # Verify
                    self._log("Verifying sharing status...")
                    verified, details = verify_sharing(source["name"], target["name"])
                    self._log(f"  Verification: {details[:200]}")

                    self.is_sharing = True
                    self.current_source = source["name"]
                    self.current_target = target["name"]

                    self.root.after(0, lambda: self.share_btn.config(
                        text="■ Stop Sharing", state=tk.NORMAL))
                    self._set_status(f"Sharing: {source['name']} → {target['name']}")
                    self._log(f"✓ Sharing active: {source['name']} → {target['name']}")
                else:
                    self.root.after(0, lambda: self.share_btn.config(
                        text="▶ Start Sharing", state=tk.NORMAL))
                    self._set_status("Sharing failed")
                    self._log(f"✗ Failed: {msg}")
                    self.root.after(0, lambda: messagebox.showerror(
                        "Sharing Failed", f"Could not enable sharing:\n{msg}"))

            except Exception as e:
                self._log(f"✗ Error: {e}")
                self._set_status("Error")
                self.root.after(0, lambda: self.share_btn.config(
                    text="▶ Start Sharing", state=tk.NORMAL))
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=_do, daemon=True).start()

    def _stop_sharing(self):
        """Stop internet sharing."""
        self.share_btn.config(state=tk.DISABLED)
        self._set_status("Stopping sharing...")
        self._log("Stopping internet sharing...")

        def _do():
            try:
                ok, msg = disable_sharing()
                self._log(f"  Disable result: {msg[:200]}")

                if self.hotspot_var.get():
                    self._log("Disabling Mobile Hotspot...")
                    ok_hp, msg_hp = disable_mobile_hotspot()
                    self._log(f"  Hotspot: {msg_hp[:200]}")

                self.is_sharing = False
                self.current_source = None
                self.current_target = None

                self.root.after(0, lambda: self.share_btn.config(
                    text="▶ Start Sharing", state=tk.NORMAL))
                self._set_status("Sharing stopped")
                self._log("✓ Sharing stopped")

            except Exception as e:
                self._log(f"✗ Error stopping: {e}")
                self.root.after(0, lambda: self.share_btn.config(
                    text="■ Stop Sharing", state=tk.NORMAL))

        threading.Thread(target=_do, daemon=True).start()

    def _check_status(self):
        """Check current ICS status."""
        self._log("Checking sharing status...")
        def _do():
            status = get_sharing_status()
            self._log(f"  Current ICS status: {status}")

            ics_ok = check_ics_service()
            self._log(f"  ICS Service: {'Running' if ics_ok else 'Stopped'}")
        threading.Thread(target=_do, daemon=True).start()

    def _on_close(self):
        """Handle window close."""
        if self.is_sharing:
            result = messagebox.askyesno(
                "Stop Sharing?",
                "Internet sharing is still active.\n\nStop sharing and exit?",
                icon="question"
            )
            if not result:
                return
            try:
                disable_sharing()
                if self.hotspot_var.get():
                    disable_mobile_hotspot()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = InternetShareApp()
    app.run()


if __name__ == "__main__":
    main()
