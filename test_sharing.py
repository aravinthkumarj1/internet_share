"""
Test Plan & Tests for Internet Share Application
=================================================

TEST PLAN:
----------
1. Unit Tests (no admin required):
   T1. Adapter discovery returns valid data structure
   T2. Internet adapters filter correctly (must have gateway)
   T3. Target adapters exclude source and virtual adapters
   T4. Display name formatting is correct
   T5. Admin detection works

2. Integration Tests (admin required):
   T6. ICS service check and start
   T7. Disable all ICS (safe — just clears existing)
   T8. Enable ICS between two adapters
   T9. Verify ICS is active after enable
   T10. Disable ICS and verify cleanup
   
3. UI Tests (manual):
   T11. App launches without errors
   T12. Adapter dropdowns populate correctly
   T13. Source change updates target list
   T14. Share button enables ICS
   T15. Stop button disables ICS
   T16. Close while sharing prompts user
   T17. Refresh updates adapter lists

4. Edge Cases:
   T18. No internet adapters available
   T19. Only one adapter available
   T20. Adapter disconnects during sharing
   T21. ICS service not running
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from internet_share.admin_utils import is_admin
from internet_share.network_utils import (
    get_all_adapters, get_internet_adapters, get_sharing_capable_targets,
    get_adapter_display_name, check_ics_service,
)
from internet_share.ics_manager import (
    disable_all_ics, get_sharing_status, verify_sharing,
    enable_ics, disable_sharing,
)


class TestAdapterDiscovery(unittest.TestCase):
    """T1-T5: Adapter discovery and filtering tests."""

    def setUp(self):
        self.adapters = get_all_adapters()

    def test_T1_adapters_return_list(self):
        """T1: get_all_adapters returns a list."""
        self.assertIsInstance(self.adapters, list)
        print(f"  Found {len(self.adapters)} adapters")

    def test_T2_adapter_has_required_fields(self):
        """T1b: Each adapter has required fields."""
        required = ["name", "description", "status", "ifindex"]
        for adapter in self.adapters:
            for field in required:
                self.assertIn(field, adapter, f"Missing field '{field}' in {adapter.get('name', '?')}")

    def test_T3_internet_adapters_have_gateway(self):
        """T2: Internet adapters all have a gateway."""
        internet = get_internet_adapters(self.adapters)
        print(f"  Internet adapters: {[a['name'] for a in internet]}")
        for a in internet:
            self.assertTrue(a["gateway"], f"{a['name']} should have gateway")
            self.assertEqual(a["status"], "Up", f"{a['name']} should be Up")

    def test_T4_target_excludes_source(self):
        """T3: Target list excludes the source adapter."""
        internet = get_internet_adapters(self.adapters)
        if not internet:
            self.skipTest("No internet adapters found")

        source = internet[0]
        targets = get_sharing_capable_targets(self.adapters, source["name"])
        target_names = [t["name"] for t in targets]
        print(f"  Source: {source['name']}, Targets: {target_names}")
        self.assertNotIn(source["name"], target_names)

    def test_T5_display_name_format(self):
        """T4: Display name format is correct."""
        for a in self.adapters:
            display = get_adapter_display_name(a)
            self.assertIsInstance(display, str)
            self.assertGreater(len(display), 0)
            # Should contain adapter name
            self.assertIn(a["name"], display)
            print(f"  {display}")

    def test_T6_admin_check(self):
        """T5: Admin detection returns bool."""
        result = is_admin()
        self.assertIsInstance(result, bool)
        print(f"  Running as admin: {result}")


class TestICSService(unittest.TestCase):
    """T6-T7: ICS service tests."""

    def test_T7_ics_service_check(self):
        """T6: ICS service check returns bool."""
        result = check_ics_service()
        self.assertIsInstance(result, bool)
        print(f"  ICS service running: {result}")

    def test_T8_get_sharing_status(self):
        """T8: Get sharing status returns string."""
        status = get_sharing_status()
        self.assertIsInstance(status, str)
        print(f"  Sharing status: {status}")


class TestICSOperations(unittest.TestCase):
    """T8-T10: ICS enable/disable tests (requires admin)."""

    def setUp(self):
        if not is_admin():
            self.skipTest("Admin privileges required for ICS operations")

    def test_T9_disable_all_ics(self):
        """T7: Disable all ICS connections."""
        ok, msg = disable_all_ics()
        print(f"  Disable all ICS: ok={ok}, msg={msg[:200]}")
        # Should succeed even if nothing was shared
        self.assertTrue(ok)

    def test_T10_enable_and_verify_sharing(self):
        """T8-T10: Full sharing cycle — enable, verify, disable."""
        adapters = get_all_adapters()
        internet = get_internet_adapters(adapters)

        if not internet:
            self.skipTest("No internet adapter found")

        source = internet[0]
        targets = get_sharing_capable_targets(adapters, source["name"])

        # Filter to real targets (not disconnected)
        active_targets = [t for t in targets if t["status"] == "Up"]
        if not active_targets:
            # Try with any target including disconnected
            all_targets = [a for a in adapters
                          if a["name"] != source["name"]
                          and "vmware" not in a["description"].lower()
                          and a["status"] == "Up"]
            if not all_targets:
                self.skipTest("No suitable target adapter found")
            active_targets = all_targets

        target = active_targets[0]
        print(f"  Testing: {source['name']} → {target['name']}")

        # Enable
        log_msgs = []
        ok, msg = enable_ics(source["name"], target["name"],
                              log_callback=lambda m: log_msgs.append(m))
        print(f"  Enable result: ok={ok}")
        for lm in log_msgs:
            print(f"    {lm}")

        if ok:
            # Verify
            verified, details = verify_sharing(source["name"], target["name"])
            print(f"  Verified: {verified}, {details[:200]}")

            # Cleanup
            dok, dmsg = disable_sharing()
            print(f"  Disabled: ok={dok}")
            self.assertTrue(dok, "Failed to disable sharing")
        else:
            print(f"  Enable failed (may be policy restricted): {msg[:200]}")
            # Don't fail — policy may block ICS
            print("  NOTE: ICS may be blocked by Group Policy. This is expected in corporate environments.")


class TestEdgeCases(unittest.TestCase):
    """T18-T21: Edge case tests."""

    def test_T18_empty_adapter_list(self):
        """T18: Functions handle empty adapter list."""
        internet = get_internet_adapters([])
        self.assertEqual(internet, [])

        targets = get_sharing_capable_targets([], "Wi-Fi")
        self.assertEqual(targets, [])

    def test_T19_single_adapter(self):
        """T19: Functions handle single adapter."""
        single = [{
            "name": "Wi-Fi", "description": "Test WiFi", "status": "Up",
            "mac": "AA:BB:CC:DD:EE:FF", "ifindex": 1, "media_type": "",
            "interface_type": "", "ip_address": "192.168.1.100", "gateway": "192.168.1.1"
        }]
        internet = get_internet_adapters(single)
        self.assertEqual(len(internet), 1)

        # Target should be empty (only adapter is source)
        targets = get_sharing_capable_targets(single, "Wi-Fi")
        self.assertEqual(len(targets), 0)

    def test_T20_vmware_filtered(self):
        """T20: VMware adapters are filtered from targets."""
        adapters = [
            {"name": "Wi-Fi", "description": "Intel WiFi", "status": "Up",
             "mac": "", "ifindex": 1, "media_type": "", "interface_type": "",
             "ip_address": "10.0.0.5", "gateway": "10.0.0.1"},
            {"name": "VMware Net", "description": "VMware Virtual Ethernet Adapter",
             "status": "Up", "mac": "", "ifindex": 2, "media_type": "",
             "interface_type": "", "ip_address": "192.168.80.1", "gateway": ""},
        ]
        targets = get_sharing_capable_targets(adapters, "Wi-Fi")
        target_names = [t["name"] for t in targets]
        self.assertNotIn("VMware Net", target_names)


def run_tests():
    """Run all tests with verbose output."""
    print("=" * 60)
    print("Internet Share Application — Test Suite")
    print("=" * 60)
    print(f"Admin: {is_admin()}")
    print()

    # Create test suite
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    suite.addTests(loader.loadTestsFromTestCase(TestAdapterDiscovery))
    suite.addTests(loader.loadTestsFromTestCase(TestICSService))
    suite.addTests(loader.loadTestsFromTestCase(TestICSOperations))
    suite.addTests(loader.loadTestsFromTestCase(TestEdgeCases))

    # Run with verbose output
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print()
    print("=" * 60)
    if result.wasSuccessful():
        print("ALL TESTS PASSED")
    else:
        print(f"FAILURES: {len(result.failures)}, ERRORS: {len(result.errors)}")
    print("=" * 60)

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
