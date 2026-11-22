# KDE Plasma Wayland support plan

**Recommendation:** target a local Plasma 6 Wayland session on a Linux DRM/KMS seat. Prove that the app can identify KWin's running GPU on a real dual-GPU machine **before enabling Apply**. Until then, KDE is a research target, not a supported configuration. Plasma X11, the login greeter, per-application render offload and choosing a primary monitor are separate problems. Keep the GTK interface; a Qt port would not change which GPU KWin uses.

## What the code can reuse

| Area | Keep | Change for KDE |
| --- | --- | --- |
| Hardware | `core/gpu.py` and `core/connectors.py`: PCI identity, DRM nodes and connected displays. | Resolve devices for the active seat; connected monitors are a warning signal, not evidence of which GPU KWin selected. |
| Detection | `detect.py` distinguishes saved preference from running adapter. | Identify the actual session, not installed binaries. Separate Mutter's udev tags and GNOME Shell journal probe from KWin evidence. |
| Writes | `helper/` and `bus.py` implement GNOME's polkit-authorized `/etc/udev/rules.d/61-mutter-primary-gpu.rules` operation. | Leave that contract intact. A KWin environment setting is not a udev rule; KDE Apply/Reset must never call the Mutter helper. |
| Presentation | `gpu_state.py`, `state.py`, `decisions.py`, `main.py` and `cli.py` already distinguish a saved choice, pending activation and a verified result. | Replace GNOME-only action gates, tag/reboot explanations and logout routing. Keep `list` and `status` read-only and usable without GTK or a display. |

Paths above are under `src/gpu_switcher/`. See the [design notes](../DESIGN.md) for the existing GNOME contract.

## 1. Prove selection and observation separately

Use one pinned KWin/Plasma Workspace package pair first. Record distro, session launcher, seat, PCI addresses, DRM card/render nodes and which GPU drives each test display.

- **Selection:** In [KWin 6.6's DRM backend](https://github.com/KDE/kwin/blob/v6.6.0/src/backends/drm/drm_backend.cpp), `KWIN_DRM_DEVICES` is an ordered **allowlist**, not just a preferred card. KWin attempts the listed paths in order; a failed open can leave a different card first, and zero usable cards can prevent login. Omitting the GPU wired to an external monitor can lose that output. [KWin 6.8's renderer selection](https://github.com/KDE/kwin/blob/v6.8.0/src/compositor.cpp) also checks render-device compatibility. Do not assume that the first configured card, compositor renderer and every output's scanout device are identical across versions.
- **Observation:** Try [Wayland linux-dmabuf default feedback](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/stable/linux-dmabuf/linux-dmabuf-v1.xml) on the current session socket. Map its `main_device` device number through `/sys/dev/char` to PCI. It describes a buffer-allocation preference; trace [how KWin supplies it](https://github.com/KDE/kwin/blob/v6.6.0/src/opengl/eglbackend.cpp) and compare it with an independent trace of KWin's actual device selection in auto and both explicit orders. The 6.6 “chose … as the primary GPU” debug message only covers automatic multi-GPU selection. Neither renderer names nor the saved list distinguish two identical GPUs reliably.
- **Go/no-go:** Enable writes only for versions/backends where evidence identifies the intended physical device for both explicit choices. Document what the indicator measures. If feedback is absent, ambiguous, or measures something different, return *unknown* and keep that configuration read-only. If the probe needs a new runtime dependency, measure its packaging impact and seek approval before adding it.

## 2. Define ownership, startup and recovery

Proposed scope: **one user's Plasma sessions**, clearly labeled in the UI, unlike the system-wide GNOME rule. Confirm that scope before implementation. If machine-wide KDE policy is required, design a separate authorized machine preference; do not have a root helper write to home directories.

Use a fixed, package-owned startup hook and a non-executable per-user preference containing a validated PCI ID. [Plasma's startup sequence](https://github.com/KDE/plasma-workspace/blob/v6.5.5/startkde/startplasma-wayland.cpp) and [environment-script loader](https://github.com/KDE/plasma-workspace/blob/v6.5.5/startkde/startplasma.cpp) provide a candidate hook before KWin starts. Prove the order with the target display manager and systemd user session; an export in the running app is too late.

1. At each login, resolve the PCI choice to a current card on the intended seat. Build the list with that card first, then the other eligible KMS cards on that seat, even if unplugged from a monitor. Persist neither `cardN` nor a static allowlist. Using resolved card paths avoids the literal colons in PCI by-path links. An unlisted, newly attached eGPU may still need a new login.
2. If no preference exists, do nothing. If the selected card is absent, the inventory is incomplete, the seat is ambiguous or an external override exists, do not export a partial list or overwrite the override. Surface the reason in status. Check for overrides that take effect *after* the hook as well; an on-disk scan alone cannot prove the effective environment.
3. Apply atomically and revalidate at write time. Reset removes only the app-owned KDE preference; preserve foreign settings, the GNOME rule and other users' choices. Verify that reset actually clears the managed value on the next login, including with a lingering systemd user manager. Do not indiscriminately unset a foreign value.
4. Provide a tested TTY/SSH recovery procedure: identify and remove only the managed preference, then start a fresh session. Test recovery from a failed graphical login before shipping. Do not restart a live KWin or offer reboot as the normal activation path.

## 3. Integrate the verified path

- First add failing session-routing tests in `tests/test_detect.py`: GNOME and Plasma on Wayland, X11, nested sessions, SSH and conflicting session evidence. Route detection, writes and logout separately. An unknown session may list hardware, never change preferences.
- Share the PCI inventory and presentation, not the backend's evidence rules. Show **saved**, **running**, and **takes effect after login** separately. Show *active* only with matching runtime evidence; report an ignored preference only when it was in effect at startup and the verified running device differs. Missing evidence is not a mismatch. With an external override, reset means “no app preference,” not necessarily “KWin auto.”
- Exercise the same snapshots through CLI and GTK tests. Keep GNOME behavior and D-Bus signatures unchanged. KDE should offer a confirmed logout only if [Plasma session management](https://github.com/KDE/plasma-workspace/blob/v6.6.0/libkworkspace/sessionmanagement.cpp) is available; otherwise instruct the user to log out manually. Cancellation must leave the session running and the saved choice pending.
- Install and remove the hook through both Debian build paths. Check `debian/control`, `debian/rules`, `Makefile` and `tools/build-deb.sh`; GNOME users should not need a Plasma desktop installed. Update English/Russian catalogs, the man page, README compatibility table and recovery instructions only for configurations that pass validation.

## Release checklist

| Gate | Passing result |
| --- | --- |
| Real hybrid host | Select each GPU in turn. Before logout the running adapter stays unchanged; after login independent evidence matches the new choice. Internal panel and external monitor wired to the other GPU work, including monitor reconnect. |
| Failure and recovery | Exercise renumbering, missing/unusable selected card, foreign override, reset, and persistent user manager. No partial allowlist, stale managed override or destructive cleanup; TTY recovery restores login. Record hotplug limitations. |
| Isolation | Another user/seat is unaffected; KDE and GNOME preferences can coexist. Unsupported sessions cannot Apply. SSH can list devices and reports unknown rather than inventing a running GPU. |
| Regression and distribution | Run CI's `GPU_SWITCHER_GUI_TESTS=1 xvfb-run -a make coverage`, `make lint`, `make lint-shell`, `make typecheck`, both Debian builds and lintian. Exercise packaged startup and reset on Plasma and the existing GNOME workflow. Xvfb verifies UI behavior, not KWin hardware selection. |

Publish the exact tested distro, KWin/Workspace versions, drivers, wiring and observation method. No real Plasma session or multi-GPU hardware validation is part of this plan's source research.
