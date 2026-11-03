"""Unit tests for the pure selection logic in selection.py.

The app must always reflect the configured state: the radio selection is
the card matching the managed rule on disk, the configured-unavailable
state when the rule pins a GPU that is not currently detected, and auto
when no rule pins a GPU (including when no GPUs are detected at all).
"""
import unittest

from gpu_switcher import detect, selection


def _gpu(card, pci, in_use=False, preferred=False):
    return detect.Gpu(card=card, node=f"/dev/dri/{card}", pci=pci,
                      vendor="1002", device="13c0", name=f"GPU {card}",
                      in_use=in_use, preferred=preferred)


def _detection(gpus, preferred_pci=None, running_card=None,
               running_card_error=None):
    return detect.Detection(gpus=gpus, running_card=running_card,
                            running_card_error=running_card_error,
                            preferred_pci=preferred_pci, conflicts=[],
                            gnome_wayland=True)


class InitialSelection(unittest.TestCase):
    def test_InitialSelection_ReturnsConfiguredCard_WhenGpuPreferred(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0"),
             _gpu("card2", "0000:12:00.0", preferred=True)],
            preferred_pci="0000:12:00.0")
        self.assertEqual(selection.initial_selection(detection), "card2")

    def test_InitialSelection_ReturnsUnavailableState_WhenConfiguredGpuMissing(self):
        # The rule pins a PCI no detected GPU carries: a distinct state,
        # not auto - the rule is still on disk.
        detection = _detection(
            [_gpu("card1", "0000:01:00.0")],
            preferred_pci="0000:99:00.0")
        self.assertEqual(selection.initial_selection(detection),
                         selection.SELECTION_UNAVAILABLE)

    def test_InitialSelection_ReturnsUnavailableState_WhenNoGpusDetected(self):
        detection = _detection([], preferred_pci="0000:99:00.0")
        self.assertEqual(selection.initial_selection(detection),
                         selection.SELECTION_UNAVAILABLE)

    def test_InitialSelection_ReturnsAuto_WhenNoRule(self):
        detection = _detection([_gpu("card1", "0000:01:00.0")])
        self.assertEqual(selection.initial_selection(detection), "auto")

    def test_InitialSelection_ReturnsAuto_WhenNoGpusDetectedAndNoRule(self):
        detection = _detection([])
        self.assertEqual(selection.initial_selection(detection), "auto")


class ConfiguredUnavailablePci(unittest.TestCase):
    def test_ConfiguredUnavailablePci_ReturnsPci_WhenRulePinsUndetectedGpu(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0")],
            preferred_pci="0000:99:00.0")
        self.assertEqual(selection.configured_unavailable_pci(detection),
                         "0000:99:00.0")

    def test_ConfiguredUnavailablePci_ReturnsNone_WhenRuleMatchesDetectedGpu(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0", preferred=True)],
            preferred_pci="0000:01:00.0")
        self.assertIsNone(selection.configured_unavailable_pci(detection))

    def test_ConfiguredUnavailablePci_ReturnsNone_WhenNoRule(self):
        detection = _detection([_gpu("card1", "0000:01:00.0")])
        self.assertIsNone(selection.configured_unavailable_pci(detection))


class SelectionAfterFailure(unittest.TestCase):
    def test_SelectionAfterFailure_ReturnsConfiguredCard_WhenPreferredExists(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0", preferred=True)],
            preferred_pci="0000:01:00.0")
        self.assertEqual(selection.selection_after_failure(detection), "card1")

    def test_SelectionAfterFailure_ReturnsUnavailableState_WhenConfiguredGpuMissing(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0")],
            preferred_pci="0000:99:00.0")
        self.assertEqual(selection.selection_after_failure(detection),
                         selection.SELECTION_UNAVAILABLE)

    def test_SelectionAfterFailure_ReturnsAuto_WhenNoRule(self):
        detection = _detection([_gpu("card1", "0000:01:00.0")])
        self.assertEqual(selection.selection_after_failure(detection), "auto")


if __name__ == "__main__":
    unittest.main()
