"""Unit tests for the pure selection logic in selection.py.

The app must always reflect the configured state: the radio selection is
the card matching the managed rule on disk, falling back to auto when no
detected GPU matches (including when no GPUs are detected at all - the
scenario that used to crash Reset).
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

    def test_InitialSelection_ReturnsAuto_WhenNoGpuMatches(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0")],
            preferred_pci="0000:99:00.0")
        self.assertEqual(selection.initial_selection(detection), "auto")

    def test_InitialSelection_ReturnsAuto_WhenNoGpusDetected(self):
        detection = _detection([])
        self.assertEqual(selection.initial_selection(detection), "auto")


class SelectionAfterFailure(unittest.TestCase):
    def test_SelectionAfterFailure_ReturnsConfiguredCard_WhenPreferredExists(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0", preferred=True)],
            preferred_pci="0000:01:00.0")
        self.assertEqual(selection.selection_after_failure(detection), "card1")

    def test_SelectionAfterFailure_ReturnsAuto_WhenNoPreferredGpu(self):
        detection = _detection(
            [_gpu("card1", "0000:01:00.0")])
        self.assertEqual(selection.selection_after_failure(detection), "auto")


if __name__ == "__main__":
    unittest.main()
