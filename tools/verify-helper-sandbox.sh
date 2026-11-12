#!/usr/bin/env bash
# Check that the helper unit's sandbox keeps /etc/udev/rules.d writable and
# nothing else. Usage: tools/verify-helper-sandbox.sh [unit-file]
#
# systemd-analyze verify only reads the unit; it cannot tell whether
# ProtectSystem=strict accidentally closed the one directory the helper needs,
# or whether a future edit opened more than intended. This runs the real
# [Service] section — copied from the unit file line by line, with only Type and
# ExecStart replaced — as a throwaway unit that reports what it could and could
# not do. Change the unit and this probe changes with it.
#
# Nothing is written to /etc/udev/rules.d: whether it is writable is asked with
# access(2), so the probe never touches the directory the helper owns. The probe's
# write attempt targets a path that ProtectSystem=strict covers, which a correct
# sandbox holds read-only, so even a failing run leaves nothing behind.
#
# The target must not be /tmp or /var/tmp: PrivateTmp=yes replaces those with
# private writable mounts that sit outside the ProtectSystem fence, so writing
# there succeeds precisely when the sandbox is working.
set -euo pipefail
umask 022

# Paths resolve from the repository, not from the caller's working directory:
# `sudo tools/...` from elsewhere should check the unit this project ships,
# not fail on a relative path. Same resolution as tools/build-deb.sh and
# tools/release.sh use at their start.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="${1:-$REPO_ROOT/debian/gpu-switcher-helper.service}"
PROBE_UNIT="gpu-switcher-sandbox-probe.service"
PROBE_UNIT_PATH="/run/systemd/system/$PROBE_UNIT"
PROBE_DIR="/run/gpu-switcher-sandbox"
PROBE_SCRIPT="$PROBE_DIR/probe.py"
WRITE_DIR="/etc/udev/rules.d"
# Outside the fence (ProtectSystem=strict mounts /etc read-only; only the
# ReadWritePaths entry above stays writable), harmless if the fence is broken:
# a four-byte file that the probe removes in the same breath. Choosing /etc and
# not, say, /usr/share also catches a ReadWritePaths= that grew to all of /etc.
PROBE_TARGET="/etc/gpu-switcher-sandbox-probe.write"

[ -r "$UNIT_SRC" ] || {
  echo "error: cannot read the unit file: $UNIT_SRC" >&2
  exit 2
}
[ "$(id -u)" = 0 ] || {
  echo "error: run as root — the probe starts a systemd unit" >&2
  exit 2
}
[ -d /run/systemd/system ] || {
  echo "error: no systemd running here" >&2
  exit 2
}
# A unit that stopped declaring its sandbox would make the probe pass by testing
# nothing at all.
for directive in ProtectSystem= ReadWritePaths=; do
  grep -q "^$directive" "$UNIT_SRC" || {
    echo "error: $UNIT_SRC no longer declares $directive* — the probe would prove nothing" >&2
    exit 2
  }
done

# A target shadowed by PrivateTmp would report an open fence while the fence holds.
case "$PROBE_TARGET" in
  /tmp/*|/var/tmp/*)
    if grep -q '^PrivateTmp=yes' "$UNIT_SRC"; then
      echo "error: $PROBE_TARGET is shadowed by PrivateTmp=yes: a private writable" >&2
      echo "       mount, outside the ProtectSystem fence — it proves nothing" >&2
      exit 2
    fi
    ;;
esac

# A unit of the same name cached from an earlier run - or a failed state left by
# one - makes systemd refuse to load the new fragment ("Device or resource busy"),
# after which systemctl reports that earlier activation's status. Release the name
# and forget the old fragment before writing anything.
systemctl stop "$PROBE_UNIT" >/dev/null 2>&1 || true
systemctl reset-failed "$PROBE_UNIT" >/dev/null 2>&1 || true
rm -f "$PROBE_UNIT_PATH"
systemctl daemon-reload >/dev/null 2>&1 || true

cleanup() {
  systemctl stop "$PROBE_UNIT" >/dev/null 2>&1 || true
  systemctl reset-failed "$PROBE_UNIT" >/dev/null 2>&1 || true
  rm -f "$PROBE_UNIT_PATH" "$PROBE_SCRIPT"
  rmdir "$PROBE_DIR" 2>/dev/null || true
  systemctl daemon-reload >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$PROBE_DIR"

cat >"$PROBE_SCRIPT" <<'PY'
"""Report what the helper unit's sandbox allows, without writing to /etc/udev.

Exit codes are the script's contract: 0 fence holds, 1 write outside succeeded,
2 the managed rule directory is not writable, 3 unexpected refusal, 4 the rule
directory does not exist.
"""

import errno
import os
import sys

WRITE_DIR = os.environ["GPU_SWITCHER_PROBE_WRITE_DIR"]
TARGET = os.environ["GPU_SWITCHER_PROBE_TARGET"]

if not os.path.isdir(WRITE_DIR):
    print(f"probe: {WRITE_DIR} does not exist")
    sys.exit(4)

# access(2) only: the helper must be able to create files there, and this asks
# without creating one.
status = 0
if os.access(WRITE_DIR, os.W_OK):
    print(f"probe: {WRITE_DIR} is writable")
else:
    print(f"probe: {WRITE_DIR} is NOT writable")
    status = 2

try:
    with open(TARGET, "w") as handle:
        handle.write("probe")
except OSError as error:
    if error.errno == errno.EROFS:
        print(f"probe: writing {TARGET} refused with EROFS")
    else:
        # EACCES or similar means a mode/ownership wall, not the sandbox fence:
        # the helper would be blocked for a different reason than ProtectSystem.
        print(f"probe: writing {TARGET} refused with {error.strerror}")
        status = 3
else:
    os.unlink(TARGET)
    print(f"probe: writing {TARGET} SUCCEEDED — the fence is open")
    status = 1

sys.exit(status)
PY

# Everything the unit declares under [Service] is inherited unchanged: the point
# is to run that configuration, not a copy of it written here. Only the process
# and its lifetime change. Drop [Install] so the probe is never started by
# anything else, and Type=oneshot so `systemctl start` waits for the verdict.
{
  echo "[Unit]"
  echo "Description=gpu-switcher sandbox probe (verification only, safe to remove)"
  echo
  awk -v probe="/usr/bin/python3 -I -B $PROBE_SCRIPT" '
    /^\[/ { in_service = ($0 == "[Service]"); if (!in_service) next }
    !in_service { next }
    /^Type=/ { print "Type=oneshot"; next }
    /^ExecStart=/ { print "ExecStart=" probe; next }
    { print }
  ' "$UNIT_SRC"
  echo "Environment=GPU_SWITCHER_PROBE_WRITE_DIR=$WRITE_DIR"
  echo "Environment=GPU_SWITCHER_PROBE_TARGET=$PROBE_TARGET"
} >"$PROBE_UNIT_PATH"

# Load the new fragment before starting it. Starting a name that is still cached
# from a previous run fails to load, and the failure would otherwise be dressed up
# with the previous run's exit status and journal lines.
systemctl daemon-reload >/dev/null 2>&1 || true
LOAD_STATE="$(systemctl show -p LoadState --value "$PROBE_UNIT")"
# Journal evidence is bounded to this activation: unbounded, a probe that never ran
# would present the previous run's findings as its own.
SINCE="$(date '+%Y-%m-%d %H:%M:%S')"

# timeout: a probe that hangs must not hang the check. For Type=oneshot,
# `systemctl start` already waits for the process, so 30 s is the ceiling.
timeout 30 systemctl start "$PROBE_UNIT" || true
RESULT="$(systemctl show -p Result --value "$PROBE_UNIT")"
STATUS="$(systemctl show -p ExecMainStatus --value "$PROBE_UNIT")"
STATUS="${STATUS//[^0-9]/}"

echo "probe unit: LoadState=${LOAD_STATE:-unknown}, Result=${RESULT:-unknown}, exit status ${STATUS:-none}"
# The probe's own words, from this activation only: they are the reason the codes
# mean what they mean.
PROBE_LINES="$(journalctl -u "$PROBE_UNIT" -o cat --no-pager -q --since "$SINCE" | grep '^probe:' || true)"
printf '%s\n' "$PROBE_LINES"

if [ "$LOAD_STATE" != loaded ] || [ -z "$PROBE_LINES" ]; then
  echo "FAIL: the probe never ran (LoadState=${LOAD_STATE:-unknown}, Result=${RESULT:-unknown}) — nothing was measured" >&2
  exit 1
fi

if [ -z "$STATUS" ]; then
  echo "FAIL: the probe never ran (Result=${RESULT:-unknown}) — check the unit for errors" >&2
  exit 1
fi

case "$STATUS" in
  0)
    if [ "$RESULT" = success ]; then
      echo "OK: $WRITE_DIR stays writable and everything else is read-only"
      exit 0
    fi
    # An exit 0 that systemd still calls a failure is not a pass.
    echo "FAIL: the probe exited 0 but systemd reports $RESULT" >&2
    exit 1
    ;;
  1) echo "FAIL: the sandbox let a process write outside $WRITE_DIR" >&2; exit 1 ;;
  2) echo "FAIL: the helper could not write its rule into $WRITE_DIR" >&2; exit 1 ;;
  3) echo "FAIL: the outside write was refused, but not by a read-only filesystem" >&2; exit 1 ;;
  4) echo "FAIL: $WRITE_DIR does not exist on this machine" >&2; exit 1 ;;
  *)
    echo "FAIL: unexpected probe exit status $STATUS (Result=${RESULT:-unknown})" >&2
    exit 1
    ;;
esac
