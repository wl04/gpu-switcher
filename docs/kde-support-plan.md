# KDE Plasma Wayland support plan

**Recommendation:** target a local Plasma 6 Wayland session on a Linux DRM/KMS seat. Prove that the app can identify the physical GPU that KWin's DRM backend treats as primary on a real dual-GPU machine **before enabling Apply**. Until then, KDE is a research target, not a supported configuration. Plasma X11, the login greeter, per-application render offload and choosing a primary monitor are separate problems. Keep the GTK interface; a Qt port would not change which GPU KWin uses.

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
- **Go/no-go:** The reference trace must identify KWin's DRM primary by PCI address in auto mode and both explicit orders. Matching addresses alone do not validate `main_device` if the renderer and DRM primary always coincide. Require either a source trace in the pinned backend tying feedback to the DRM primary, or a test where the roles diverge and feedback still identifies the primary. Otherwise report *unknown* and keep that configuration read-only. Seek approval before adding a probe dependency, with its packaging cost.

## 2. Define ownership, startup and recovery

Proposed scope: **one user's Plasma sessions on one named seat**, clearly labeled in the UI. Key preferences by user and seat: a choice on seat B must not replace the same user's choice on seat A. Confirm that scope before implementation. The GNOME rule remains system-wide; do not have a root helper write to home directories.

Use a fixed, package-owned startup hook and a non-executable per-user preference containing a validated PCI ID. [Plasma's startup sequence](https://github.com/KDE/plasma-workspace/blob/v6.5.5/startkde/startplasma-wayland.cpp) and [environment-script loader](https://github.com/KDE/plasma-workspace/blob/v6.5.5/startkde/startplasma.cpp) provide a candidate hook before KWin starts. Prove the order with the target display manager and systemd user session; an export in the running app is too late.

1. Check the launcher, KWin/Workspace versions and backend at every login, not just at Apply. On an unvalidated combination, preserve the saved preference without applying it. With no preference for this user and seat, export nothing.
2. Resolve the saved PCI ID to a current card and put it first, followed by every other eligible KMS card on the seat, including cards with no connected monitor. The display inventory can skip unclassified cards; it is not proof of completeness. If any candidate cannot be accounted for, the selected card is unavailable, or the seat is ambiguous, export nothing. Resolve card paths at login to avoid literal colons in PCI by-path links; persist neither `cardN` nor the allowlist. A newly attached eGPU may need a new login.
3. Preserve foreign overrides. Make the hook's decision (exported or skipped, and why) available to `status` for that session. An export is not proof of what reached KWin: check for later overrides and report *unknown* if the effective value cannot be verified.
4. Apply atomically and revalidate at write time. Reset removes only this user's preference for this seat; it must not change the running session, the GNOME rule or foreign settings. Prefer a value confined to the KWin launch. If it can survive in a user manager, demonstrate that recovery can clear the app's value without unsetting someone else's. Keep the launcher read-only until that is proven. Document recovery from TTY/SSH; do not restart a live KWin or offer reboot as normal activation.

## 3. Integrate the verified path

- First add failing session-routing tests in `tests/test_detect.py`: GNOME and Plasma on Wayland, X11, nested sessions, SSH and conflicting session evidence. Route detection, writes and logout separately. An unknown session may list hardware, never change preferences.
- Test GTK activation as well as detection: with GNOME and Plasma sessions sharing a user's bus and one window already open, launch from the other session. Open a window for the calling session or refuse; never offer Apply or logout against the first session.
- Share the PCI inventory and presentation, not the backend's evidence rules. Show **saved**, **running**, and **takes effect after login** separately. Show *active* only with matching runtime evidence; report an ignored preference only when it was in effect at startup and the verified running device differs. Missing evidence is not a mismatch. With an external override, reset means “no app preference,” not necessarily “KWin auto.”
- Exercise the same snapshots through CLI and GTK tests. Keep GNOME behavior and D-Bus signatures unchanged. KDE should offer a confirmed logout only if [Plasma session management](https://github.com/KDE/plasma-workspace/blob/v6.6.0/libkworkspace/sessionmanagement.cpp) is available; otherwise instruct the user to log out manually. Cancellation must leave the session running and the saved choice pending.
- Install and remove the hook through both Debian build paths. Check `debian/control`, `debian/rules`, `Makefile` and `tools/build-deb.sh`; GNOME users should not need a Plasma desktop installed. Once a configuration passes validation, check the installed desktop entry (English and Russian), Debian package description and CLI `--help` alongside the catalogs, man page, README compatibility table and recovery instructions. Advertise only validated configurations.

## Release checklist

| Gate | Passing result |
| --- | --- |
| Real hybrid host | Select each GPU in turn. Before logout the running adapter stays unchanged; after login the verified DRM primary matches the choice by PCI address. The internal panel and a monitor wired to the other GPU work, including after reconnect. |
| Startup guards | Save a choice, upgrade to an unvalidated package pair and log in without opening the app: the preference remains but the hook exports nothing. With a valid selection and an unclassifiable second card on the seat, it exports nothing rather than a partial list. Also exercise renumbering, missing/unusable selected cards and foreign overrides. Record hotplug limitations. |
| Failed-login recovery | Test with an unusable managed allowlist already imported into a lingering user manager, or demonstrate that the launcher cannot import it there. After a failed graphical login, the documented TTY/SSH steps restore login without deleting a foreign override. |
| Fresh-process status | Reconnect an eGPU that was absent at login: a fresh `status` and reopened window report a skipped choice, not one KWin ignored. Reset an effective choice before logout: both show no saved preference without claiming the running override has cleared. After login verify no managed value remains. |
| Isolation | Save different choices on two seats under the same UID, then log in again on each: both choices survive. Another user's settings and the GNOME rule are untouched. Unsupported sessions cannot Apply. SSH can list devices and reports unknown rather than inventing a running GPU. |
| Regression and distribution | Run CI's `GPU_SWITCHER_GUI_TESTS=1 xvfb-run -a make coverage`, `make lint`, `make lint-shell`, `make typecheck`, both Debian builds and lintian. Exercise packaged startup and reset on Plasma and the existing GNOME workflow. Xvfb verifies UI behavior, not KWin hardware selection. |

Publish the exact tested distro, KWin/Workspace versions, drivers, wiring and observation method. No real Plasma session or multi-GPU hardware validation is part of this plan's source research.
