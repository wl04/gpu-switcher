"""Entry point: python3 -I -B -m gpu_switcher.helper.

The systemd unit (debian/gpu-switcher-helper.service) starts the helper
this way; the D-Bus service file falls back to the same command via
Exec=. The real service logic lives in gpu_switcher.helper.service (and
its auth/udev/rule_store companions), so this module only runs main().
"""

from gpu_switcher.helper.service import main

if __name__ == "__main__":
    main()
