"""HelperFailure: an expected, localized operation failure.

The service handlers raise it for every failure the user must be shown
(conflict rules, invalid PCI, symlink refusal, backup/unlink failures);
the dispatcher catches it once and replies with the operation-failure
tuple (ok=False). Unexpected exceptions are *not* HelperFailure: they are
logged and replied with the internal-error D-Bus error.
"""


class HelperFailure(Exception):
    """Expected operation failure; args[0] is the user-facing message."""
