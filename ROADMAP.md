# Roadmap

Ideas, roughly in priority order. Not a promise; see the
[issue tracker](https://github.com/wl04/gpu-switcher/issues) for discussion.

- **Clear state** — an explicit banner: "running now" vs "configured for next
  login" vs "reboot required", derived from the existing detection data
  (running primary, rule on disk, last udev-refresh result).
- **Verification at startup** — confirm after login whether GNOME actually
  picked the configured GPU (no session autostart).
- **Per-GPU monitor display** — connected DRM connectors from `/sys`, so the
  choice is made with context.
- **Tested configurations** — verified distro / GNOME / GPU combinations and
  whether logging out alone is sufficient; extended with real-world reports.
- **KDE Plasma and X11 support.**
- **More languages.**
