"""Unit tests for reading the display outputs of a DRM card out of sysfs.

A fake /sys/class/drm is built per test: the assertions are about which entries
count as outputs, how their status is read and what a missing answer means.
"""

import os
import tempfile
import unittest

from gpu_switcher.core.connectors import (
    Connector,
    DisplayState,
    connectors,
    display_state,
)


def _connector_dir(root: str, name: str, status: str | None) -> str:
    path = os.path.join(root, name)
    os.makedirs(path)
    if status is not None:
        with open(os.path.join(path, "status"), "w", encoding="utf-8") as f:
            f.write(f"{status}\n")
    return path


class ListConnectors(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.drm = self._tmp.name

    def test_connectors_lists_the_card_connector_entries_with_a_stable_order(
        self,
    ) -> None:
        _connector_dir(self.drm, "card1-HDMI-A-1", "connected")
        _connector_dir(self.drm, "card1-DP-1", "disconnected")
        _connector_dir(self.drm, "card2-DP-1", "connected")
        self.assertEqual(
            [c.name for c in connectors("card1", drm_dir=self.drm)],
            ["DP-1", "HDMI-A-1"],
        )
        self.assertEqual(
            [c.card for c in connectors("card1", drm_dir=self.drm)],
            ["card1", "card1"],
        )

    def test_connectors_skips_writeback_entries_because_no_monitor_plugs_into_one(
        self,
    ) -> None:
        _connector_dir(self.drm, "card1-HDMI-A-1", "connected")
        _connector_dir(self.drm, "card1-Writeback-1", "unknown")
        found = connectors("card1", drm_dir=self.drm)
        self.assertEqual([c.name for c in found], ["HDMI-A-1"])

    def test_connectors_skips_connectors_of_other_cards_and_render_nodes(
        self,
    ) -> None:
        _connector_dir(self.drm, "card2-DP-1", "connected")
        _connector_dir(self.drm, "card10-DP-1", "connected")
        os.makedirs(os.path.join(self.drm, "renderD128"))
        os.makedirs(os.path.join(self.drm, "card1"))
        self.assertEqual([c.name for c in connectors("card1", drm_dir=self.drm)], [])
        self.assertEqual(
            [c.name for c in connectors("card10", drm_dir=self.drm)], ["DP-1"]
        )

    def test_connectors_carries_the_status_the_kernel_reports(self) -> None:
        _connector_dir(self.drm, "card1-DP-1", "disconnected")
        found = connectors("card1", drm_dir=self.drm)
        self.assertEqual(
            found, [Connector(card="card1", name="DP-1", status="disconnected")]
        )

    def test_connectors_is_empty_when_the_card_has_no_connector_entries(self) -> None:
        _connector_dir(self.drm, "card1", "")
        self.assertEqual(connectors("card1", drm_dir=self.drm), [])

    def test_connectors_is_empty_when_sysfs_cannot_be_listed(self) -> None:
        self.assertEqual(
            connectors("card1", drm_dir=os.path.join(self.drm, "gone")), []
        )


class DisplayStateOfCard(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.drm = self._tmp.name

    def test_display_state_connected_when_any_output_reports_connected(self) -> None:
        _connector_dir(self.drm, "card1-DP-1", "disconnected")
        _connector_dir(self.drm, "card1-HDMI-A-1", "connected")
        self.assertIs(display_state("card1", drm_dir=self.drm), DisplayState.CONNECTED)

    def test_display_state_none_when_every_output_reports_disconnected(self) -> None:
        _connector_dir(self.drm, "card1-DP-1", "disconnected")
        _connector_dir(self.drm, "card1-HDMI-A-2", "disconnected")
        self.assertIs(display_state("card1", drm_dir=self.drm), DisplayState.NONE)

    def test_display_state_unknown_when_the_card_has_no_outputs_to_ask(self) -> None:
        # A card with no connector entries says nothing about displays; reporting
        # "no display" would turn a kernel without connector reporting into a
        # warning that the user has no screen.
        _connector_dir(self.drm, "card1", "")
        self.assertIs(display_state("card1", drm_dir=self.drm), DisplayState.UNKNOWN)

    def test_display_state_unknown_when_sysfs_cannot_be_listed(self) -> None:
        self.assertIs(
            display_state("card1", drm_dir=os.path.join(self.drm, "gone")),
            DisplayState.UNKNOWN,
        )

    def test_display_state_unknown_when_an_output_reports_a_status_that_means_nothing(
        self,
    ) -> None:
        # Drivers use "unknown" for outputs they cannot probe (virtual and
        # headless ones); that is not evidence of a free connector.
        _connector_dir(self.drm, "card1-DP-1", "disconnected")
        _connector_dir(self.drm, "card1-DP-2", "unknown")
        self.assertIs(display_state("card1", drm_dir=self.drm), DisplayState.UNKNOWN)

    def test_display_state_unknown_when_an_output_status_cannot_be_read(self) -> None:
        path = _connector_dir(self.drm, "card1-DP-1", None)
        os.symlink(
            os.path.join(self.drm, "missing-target"), os.path.join(path, "status")
        )
        _connector_dir(self.drm, "card1-DP-2", "disconnected")
        self.assertIs(display_state("card1", drm_dir=self.drm), DisplayState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
