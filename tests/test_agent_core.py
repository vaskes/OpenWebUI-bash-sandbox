"""The pure core of the agent: the privilege-mode helpers.

``parse_capability_status`` and ``sudo_posture`` answer one question:
would ``sudo su`` actually work in this container? The answer depends
on two kernel-level settings that no amount of code in the image can
change, which is why the check exists at all.
"""

from __future__ import annotations

import unittest

from agent_core import parse_capability_status, sudo_posture

# A realistic /proc/self/status blob. CapBnd is the bounding set as a
# hex bitmask; a container with Docker's default caps has bits set,
# and one with `cap_drop: ALL` has none.
STATUS_OK = """
Name:\tpython3
Uid:\t1000\t1000\t1000\t1000
CapInh:\t0000000000000000
CapPrm:\t0000000000000000
CapEff:\t0000000000000000
CapBnd:\t00000000a80425fb
NoNewPrivs:\t0
""".replace("  ", "\t")

STATUS_NO_NEW_PRIVS = STATUS_OK.replace("NoNewPrivs:\t0", "NoNewPrivs:\t1")
STATUS_EMPTY_SET = STATUS_OK.replace("CapBnd:\t00000000a80425fb", "CapBnd:\t0000000000000000")


class TestParseCapabilityStatus(unittest.TestCase):
    def test_reads_both_fields(self) -> None:
        self.assertEqual(parse_capability_status(STATUS_OK), (0, 0x00000000A80425FB))

    def test_reads_the_flag(self) -> None:
        self.assertEqual(parse_capability_status(STATUS_NO_NEW_PRIVS)[0], 1)

    def test_reads_an_empty_bounding_set(self) -> None:
        self.assertEqual(parse_capability_status(STATUS_EMPTY_SET)[1], 0)

    def test_a_missing_field_counts_as_the_safe_value(self) -> None:
        """An unreadable status file must never read as "escalation works"."""
        no_new_privs, cap_bnd = parse_capability_status("Name:\tpython3\n")
        self.assertEqual(no_new_privs, 1)
        self.assertEqual(cap_bnd, 0)

    def test_a_garbage_value_counts_as_the_safe_value(self) -> None:
        no_new_privs, cap_bnd = parse_capability_status(
            "NoNewPrivs:\tnot-a-number\nCapBnd:\tZZZZ\n"
        )
        self.assertEqual(no_new_privs, 1)
        self.assertEqual(cap_bnd, 0)


class TestSudoPosture(unittest.TestCase):
    def test_it_works_when_both_settings_allow_it(self) -> None:
        self.assertEqual(sudo_posture(0, 0xA80425FB), "ok")

    def test_no_new_privileges_blocks_the_setuid_bit(self) -> None:
        verdict = sudo_posture(1, 0xA80425FB)
        self.assertNotEqual(verdict, "ok")
        self.assertIn("no-new-privileges", verdict)

    def test_an_empty_capability_set_blocks_su(self) -> None:
        verdict = sudo_posture(0, 0)
        self.assertNotEqual(verdict, "ok")
        self.assertIn("CAP_SETUID", verdict)

    def test_both_blocked_reports_the_first_reason(self) -> None:
        """The first reason is the one the operator has to fix first."""
        verdict = sudo_posture(1, 0)
        self.assertIn("no-new-privileges", verdict)


if __name__ == "__main__":
    unittest.main()
