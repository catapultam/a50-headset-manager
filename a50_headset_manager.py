#!/usr/bin/env python3
"""
Monitor A50 headset and auto-switch audio inputs/outputs.

Audio Output Priority:
1. A50 headset (when powered on and off the dock)
2. HDMI output (when a monitor with audio is connected)
3. Internal speaker (analog output as fallback)

Audio Input Priority:
1. A50 headset microphone (when headset is active)
2. Internal microphone array (digital mic, when headset docked/disconnected)
3. External microphone (analog input as fallback)

This script continuously monitors the A50 headset status via USB and
automatically switches the system's default audio sink and source based
on whether the headset is being worn, docked, or disconnected.
"""

import glob
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass

from usb.core import USBError

from eh_fifty import Device, DeviceNotConnected

# A50 headset sink/source names (device-specific, won't change)
HEADSET_SINK = "alsa_output.usb-Astro_Gaming_Astro_A50-00.stereo-game"
HEADSET_SOURCE = "alsa_input.usb-Astro_Gaming_Astro_A50-00.mono-chat"

# Hard cap on every external audio-tool call (pactl/pw-cli/wpctl). Without this,
# a wedged PipeWire graph -- e.g. the A50 dropping its PCM endpoints and leaving
# object enumeration stuck -- freezes the call indefinitely, and the whole
# monitor loop blocks with it (observed: pw-cli hung for minutes). On timeout we
# log and return None so the loop keeps polling and recovers on its own.
SUBPROCESS_TIMEOUT = 5  # seconds

# A50 base station USB id (the same device that eh_fifty opens)
DOCK_VENDOR_ID = 0x9886
DOCK_PRODUCT_ID = 0x002C

# PipeWire's placeholder sink when no real sink exists (e.g. just after a
# restart). Never use it as a fallback: audio sent to it is lost.
DUMMY_SINK = "auto_null"

# Self-repair of the audio path. Two failures were observed:
# 1. The base station firmware hangs: the kernel logs
#    "usb_set_interface failed (-110)" for the A50 audio interfaces, PipeWire
#    retries the A50 sink without end, and PipeWire clients become too slow.
#    A USB reset of the base station fixes this.
# 2. PipeWire stops answering all clients, but its processes stay alive.
#    A restart of the PipeWire services fixes this. The cause is not known.
#    It starts some seconds after a headset state change, with no kernel
#    error. It does not occur on each change, and a manual default sink
#    change does not cause it. Without a repair, it stopped after about
#    10 minutes (2026-09-03).
# At the second failed check, the daemon saves a stall log to STALL_LOG_DIR,
# to find the cause of failure 2. It does this at most once in
# STALL_CAPTURE_INTERVAL. The stall log contains the recorder, the threads and
# stacks of the PipeWire services, the recent logs, pw-dump, and the ALSA and
# USB audio streams. eu-stack stops each thread for a short time (about
# 0.03 s for each service, without debuginfod).
# Step 1: HEALTH_FAILURE_LIMIT failed checks in sequence, during at least
# REPAIR_MIN_SECONDS. If the kernel logged an error for the base station in
# the last KERNEL_ERROR_WINDOW, do a USB reset (failure 1). If not, restart
# only WirePlumber: apps stay connected to PipeWire. Playback can stop for
# a short time.
# (A USB reset without a kernel error did not help, and the A50 devices
# then failed in PipeWire.)
# Step 2 (PipeWire restart): at least
# 2 * HEALTH_FAILURE_LIMIT failed checks in total, and at least
# 2 * REPAIR_MIN_SECONDS since the first failure, and at least
# REPAIR_MIN_SECONDS since step 1. After a restart the cycle starts again. If the fault stays, the
# minimum time between restarts is AUDIO_RESTART_COOLDOWN, and each later
# wait is twice the one before, up to AUDIO_RESTART_COOLDOWN_MAX. A good check
# resets it.
HEALTH_CHECK_INTERVAL = 10  # seconds between health checks while healthy
HEALTH_FAILURE_LIMIT = 3  # failed checks in sequence before each repair step
REPAIR_MIN_SECONDS = 15  # seconds of failures before step 1 (2x for step 2)
USB_RESET_TIMEOUT = 10  # seconds; limit for the USB reset helper process
AUDIO_RESTART_COOLDOWN = 600  # seconds; minimum time between PipeWire restarts
AUDIO_RESTART_COOLDOWN_MAX = 3600  # seconds
AUDIO_RESTART_TIMEOUT = 30  # seconds; limit for the systemctl restart command
AUDIO_SERVICES = ["wireplumber", "pipewire", "pipewire-pulse"]
SESSION_MANAGER = "wireplumber"  # restarted alone in step 1 (no USB error)
POST_RESTART_DELAY = 5  # seconds; wait after a restart before routing again
KERNEL_ERROR_WINDOW = 60  # seconds of kernel log to search for USB errors
STALL_LOG_DIR = os.path.expanduser("~/.local/state/a50-headset-manager")
STALL_LOG_KEEP = 20  # number of stall logs to keep
STALL_STACK_TIMEOUT = 5  # seconds; limit for each eu-stack call
STALL_CAPTURE_INTERVAL = 600  # seconds; minimum time between captures
STALL_LOG_WINDOW = 180  # seconds of journal to copy into a stall log
RECORD_LINES = 300  # lines in the recorder (about one each second)
HEALTH_SLOW_SECONDS = 2  # log a health check that passes but takes longer

# Retry of a sink or source switch that failed: first retry after
# ROUTE_RETRY_MIN seconds, then the wait doubles up to ROUTE_RETRY_MAX.
# After ROUTE_RETRY_LIMIT seconds the daemon stops the retries.
ROUTE_RETRY_MIN = 2  # seconds
ROUTE_RETRY_MAX = 60  # seconds
ROUTE_RETRY_LIMIT = 600  # seconds

# While the dock is disconnected, the reconnect wait grows to max_backoff.
# While a retry or a repair is in progress, the wait is at most this value.
BUSY_POLL_MAX = 2  # seconds


def run_audio_cmd(
    cmd: list[str], check: bool = False, timeout: float = SUBPROCESS_TIMEOUT,
) -> subprocess.CompletedProcess | None:
    """Run an external audio command with a hard timeout.

    Returns the CompletedProcess on success, or None if the command timed out,
    the tool was missing, or (when check=True) it exited non-zero. Callers treat
    None as "no data / switch failed" and retry on the next poll rather than
    blocking forever on an unresponsive PipeWire.
    """
    try:
        return subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout, check=check,
        )
    except subprocess.TimeoutExpired:
        print(f"Timeout after {timeout}s: {' '.join(cmd)}", flush=True)
        return None
    except subprocess.CalledProcessError as e:
        print(f"Command failed ({e.returncode}): {' '.join(cmd)}", flush=True)
        return None
    except FileNotFoundError:
        print(f"Command not found: {cmd[0]}", flush=True)
        return None


@dataclass
class SinkInfo:
    """Information about an audio sink and its availability."""
    name: str
    sink_type: str  # "hdmi", "analog", "usb", "other"
    is_available: bool  # Port-level availability (for HDMI: monitor connected)


@dataclass
class SourceInfo:
    """Information about an audio source (microphone) and its type."""
    name: str
    source_type: str  # "internal_mic", "external_mic", "monitor", "usb", "other"


def classify_sink(sink_name: str) -> str:
    """
    Classify a sink by its type based on name patterns.

    Returns: "hdmi", "analog", "usb", or "other"
    """
    name_lower = sink_name.lower()
    if "hdmi" in name_lower:
        return "hdmi"
    elif "analog" in name_lower or "speaker" in name_lower:
        return "analog"
    elif "usb" in name_lower:
        return "usb"
    else:
        return "other"


def get_sinks_with_port_availability() -> list[SinkInfo]:
    """
    Parse `pactl list sinks` to get sink names with port-level availability.

    For HDMI sinks, port availability indicates whether a monitor is actually
    connected. For other sinks, we consider them always available (they're
    physical devices that don't depend on external connections).

    Returns a list of SinkInfo objects with availability status.
    """
    result = run_audio_cmd(["pactl", "list", "sinks"])
    if result is None:
        return []

    sinks = []
    current_name = None
    port_availabilities = []  # Track all port availabilities for current sink
    in_ports_section = False

    def save_current_sink():
        """Helper to save accumulated sink info."""
        if current_name:
            sink_type = classify_sink(current_name)
            # For HDMI, use port availability; for others, always available
            if sink_type == "hdmi":
                # HDMI is available if any port shows available
                is_available = any(port_availabilities)
            else:
                # Non-HDMI sinks (analog, etc.) are always available
                is_available = True
            sinks.append(SinkInfo(current_name, sink_type, is_available))

    for line in result.stdout.splitlines():
        stripped = line.strip()

        # Detect start of new sink block - save previous sink first
        # This handles the fact that State: comes before Name: in pactl output
        if stripped.startswith("Sink #"):
            save_current_sink()
            current_name = None
            port_availabilities = []
            in_ports_section = False

        # Detect sink name
        elif stripped.startswith("Name:"):
            current_name = stripped.split(":", 1)[1].strip()

        # Detect ports section
        elif stripped.startswith("Ports:"):
            in_ports_section = True

        # Detect end of ports section (next top-level property)
        elif in_ports_section and not line.startswith("\t\t") and line.startswith("\t"):
            if not stripped.startswith("Port:") and ":" in stripped:
                in_ports_section = False

        # Parse port availability (format varies, handle both styles)
        # Style 1: "[Out] HDMI1: ... (type: HDMI, priority: 1100, availability group: ..., not available)"
        # Style 2: "Port: HDMI Output (type: HDMI, priority: 0, available: yes)"
        elif in_ports_section and "available" in stripped.lower():
            # Check for unavailable FIRST (order matters - "not available" contains "available")
            if re.search(r'\bnot available\b|\bavailable:\s*no\b', stripped, re.IGNORECASE):
                port_availabilities.append(False)
            elif re.search(r'(?<!\bnot )\bavailable\)|\bavailable:\s*yes\b', stripped, re.IGNORECASE):
                port_availabilities.append(True)

    # Don't forget the last sink
    save_current_sink()

    return sinks


def get_best_fallback_sink() -> str | None:
    """
    Find the best available fallback sink using dynamic detection.

    Priority order:
    1. HDMI sinks with a connected monitor (port available)
    2. Analog/speaker sinks (internal speaker)

    Returns the sink name or None if nothing suitable found.
    """
    sinks = get_sinks_with_port_availability()

    # Filter out the A50 headset sink - we're looking for fallbacks
    sinks = [s for s in sinks if s.name not in (HEADSET_SINK, DUMMY_SINK)]

    # First priority: HDMI with available port (monitor connected)
    for sink in sinks:
        if sink.sink_type == "hdmi" and sink.is_available:
            return sink.name

    # Second priority: Analog/speaker (internal speaker)
    for sink in sinks:
        if sink.sink_type == "analog":
            return sink.name

    # Last resort: Any other available sink
    for sink in sinks:
        if sink.is_available and sink.sink_type != "usb":
            return sink.name

    return None


def classify_source(source_name: str) -> str:
    """
    Classify a source (microphone) by its type based on name patterns.

    Returns: "internal_mic", "external_mic", "monitor", "usb", or "other"
    """
    name_lower = source_name.lower()

    # Monitor sources are loopback from sinks, not real microphones
    if ".monitor" in name_lower:
        return "monitor"

    # USB sources (like A50 headset mic)
    if "usb" in name_lower:
        return "usb"

    # Internal digital microphone (mic array) - typically named Mic1 or "digital"
    # These are built into laptops
    if "mic1" in name_lower or "digital" in name_lower:
        return "internal_mic"

    # External analog microphone input (Mic2, stereo mic, analog)
    # These are typically 3.5mm jack inputs
    if "mic2" in name_lower or "mic" in name_lower or "analog" in name_lower:
        return "external_mic"

    return "other"


def get_sources() -> list[SourceInfo]:
    """
    Parse `pactl list sources` to get source names and types.

    Returns a list of SourceInfo objects.
    """
    result = run_audio_cmd(["pactl", "list", "sources"])
    if result is None:
        return []

    sources = []
    current_name = None

    for line in result.stdout.splitlines():
        stripped = line.strip()

        # Detect start of new source block
        if stripped.startswith("Source #"):
            if current_name:
                source_type = classify_source(current_name)
                sources.append(SourceInfo(current_name, source_type))
            current_name = None

        # Detect source name
        elif stripped.startswith("Name:"):
            current_name = stripped.split(":", 1)[1].strip()

    # Don't forget the last source
    if current_name:
        source_type = classify_source(current_name)
        sources.append(SourceInfo(current_name, source_type))

    return sources


def get_best_fallback_source() -> str | None:
    """
    Find the best available fallback microphone using dynamic detection.

    Priority order:
    1. Internal digital microphone (laptop mic array)
    2. External analog microphone input

    Returns the source name or None if nothing suitable found.
    """
    sources = get_sources()

    # Filter out the A50 headset source and monitor sources
    sources = [s for s in sources if s.name != HEADSET_SOURCE and s.source_type != "monitor"]

    # First priority: Internal digital microphone (mic array)
    for source in sources:
        if source.source_type == "internal_mic":
            return source.name

    # Second priority: External analog microphone
    for source in sources:
        if source.source_type == "external_mic":
            return source.name

    # Last resort: Any other non-USB source
    for source in sources:
        if source.source_type not in ("usb", "monitor"):
            return source.name

    return None


def get_node_id(node_name: str) -> str | None:
    """Look up PipeWire node ID by name."""
    result = run_audio_cmd(["pw-cli", "ls", "Node"])
    if result is None:
        return None
    current_id = None
    for line in result.stdout.splitlines():
        if line.startswith("\tid"):
            current_id = line.split()[1].rstrip(",")
        if f'node.name = "{node_name}"' in line:
            return current_id
    return None


def set_default_sink(node_name: str) -> bool:
    """Set default audio sink by name."""
    node_id = get_node_id(node_name)
    if node_id:
        if run_audio_cmd(["wpctl", "set-default", node_id], check=True) is None:
            return False
        return True
    return False


def set_default_source(node_name: str) -> bool:
    """Set default audio source by name."""
    node_id = get_node_id(node_name)
    if node_id:
        if run_audio_cmd(["wpctl", "set-default", node_id], check=True) is None:
            return False
        return True
    return False


def audio_healthy() -> bool:
    """Return True if PipeWire answers both query paths that a switch uses
    (pactl and pw-cli), each within SUBPROCESS_TIMEOUT.

    In the base station hang, pw-cli was the slow call, so both are tested.
    """
    return (run_audio_cmd(["pactl", "list", "short", "sinks"], check=True) is not None
            and run_audio_cmd(["pw-cli", "ls", "Node"], check=True) is not None)


# Runs in a separate process with a time limit, so an unexpected error or a
# stall in libusb cannot stop the daemon. (If the reset ioctl itself hangs in
# the kernel, the kill after the timeout waits for it.) pyusb reports
# ENODEV/ENOENT when the reset makes the device enumerate again; that is a
# successful reset.
_USB_RESET_SCRIPT = """
import errno, sys, usb.core, usb.util
dev = usb.core.find(idVendor=%d, idProduct=%d)
if dev is None:
    sys.exit(2)
try:
    dev.reset()
except usb.core.USBError as e:
    if e.errno not in (errno.ENODEV, errno.ENOENT):
        print(e, file=sys.stderr)
        sys.exit(1)
finally:
    usb.util.dispose_resources(dev)
""" % (DOCK_VENDOR_ID, DOCK_PRODUCT_ID)


def reset_dock_usb() -> bool:
    """USB-reset the A50 base station. The caller must close its Device first.

    Needs write access to the USB device node (the udev rule gives it).
    Returns True if the reset was done.
    """
    result = run_audio_cmd(
        [sys.executable, "-c", _USB_RESET_SCRIPT], timeout=USB_RESET_TIMEOUT,
    )
    if result is None:
        return False
    if result.returncode == 2:
        print("  USB reset: base station not found", flush=True)
    elif result.returncode != 0:
        print(f"  USB reset failed: {result.stderr.strip()}", flush=True)
    return result.returncode == 0


def restart_audio_services(services: list[str] = AUDIO_SERVICES) -> bool:
    """Restart the given audio user services. Returns True on success."""
    result = run_audio_cmd(
        ["systemctl", "--user", "restart", *services],
        check=True, timeout=AUDIO_RESTART_TIMEOUT,
    )
    return result is not None


def dock_usb_paths() -> list[str]:
    """Return the sysfs names of the base station (for example "5-1.1.2.2")."""
    paths = []
    for vendor_file in glob.glob("/sys/bus/usb/devices/*/idVendor"):
        dev_dir = os.path.dirname(vendor_file)
        try:
            with open(vendor_file) as f:
                vendor = int(f.read(), 16)
            with open(os.path.join(dev_dir, "idProduct")) as f:
                product = int(f.read(), 16)
        except (OSError, ValueError):
            continue
        if (vendor, product) == (DOCK_VENDOR_ID, DOCK_PRODUCT_ID):
            paths.append(os.path.basename(dev_dir))
    return paths


def dock_usb_errors() -> bool:
    """Return True if the kernel logged a USB error for the base station in
    the last KERNEL_ERROR_WINDOW seconds (for example
    "usb 5-1.1.2.2: 4:1: usb_set_interface failed (-110)")."""
    paths = dock_usb_paths()
    if not paths:
        return False
    result = run_audio_cmd(
        ["journalctl", "-k", f"--since=-{KERNEL_ERROR_WINDOW}s",
         "-o", "cat", "--no-pager"],
    )
    if result is None:
        print("  Could not read the kernel log", flush=True)
        return False
    if not result.stdout and result.stderr:
        # For example: the user cannot read the kernel log
        print(f"  Kernel log: {result.stderr.strip()}", flush=True)
        return False
    for line in result.stdout.splitlines():
        if not any(f" {p}:" in line for p in paths):
            continue
        line = line.lower()
        if "fail" in line or "error" in line or "(-" in line:
            return True
    return False


# Writes the threads and the stacks of each audio service. Runs in the
# background, so a slow eu-stack does not stop the daemon. DEBUGINFOD_URLS is
# cleared: a download of debug data while a thread is stopped would make the
# stall longer. eu-stack cannot stop a thread that waits in the kernel
# (state D), and its stop signal can then stay pending. For this reason, if
# the second thread list has a thread in state D, the script does not use
# eu-stack.
# SIGCONT after eu-stack removes a pending stop signal.
#
# Each thread line: ID, name, kernel wait point, state, user and system CPU
# time (clock ticks). The thread list is written two times, 1 s apart: a
# thread that uses CPU and a thread that waits have the same wait point, but
# only the first has a CPU time that increases.
#
# Then the script writes the last STALL_LOG_WINDOW seconds of the kernel log
# and of the audio service and daemon logs. It removes the many "split lock"
# lines from the kernel log. Then it writes the pw-dump output. Only pw-dump
# needs PipeWire. The ALSA and USB audio stream data is last. A read of an
# ALSA status file can wait for a kernel lock (for example, during a USB
# hang). timeout cannot stop a read in state D. Then only this last part of
# the stall log is lost.
_STALL_CAPTURE_SCRIPT = """
threads() {
    blocked=no
    for t in /proc/"$1"/task/*; do
        set -- $(sed 's/.*) //' "$t/stat")
        [ "$1" = D ] && blocked=yes
        echo "${t##*/} $(cat "$t/comm") $(cat "$t/wchan") $1 utime=${12} stime=${13}"
    done
}
for svc in "$@"; do
    pid=$(systemctl --user show -p MainPID --value "$svc")
    echo "== $svc (pid $pid)"
    [ -n "$pid" ] && [ "$pid" != 0 ] || continue
    threads "$pid"
    sleep 1
    echo "-- 1 s later:"
    threads "$pid"
    if [ "$blocked" = yes ]; then
        echo "A thread is in state D: no stacks"
        continue
    fi
    DEBUGINFOD_URLS= timeout -k 1 %(stack)d eu-stack -p "$pid" 2>&1
    kill -CONT "$pid" 2>/dev/null
done
echo "== Kernel log"
journalctl -k --since=-%(window)ds -o short-precise --no-pager | grep -v 'split lock'
echo "== Audio service log"
journalctl --user -u wireplumber -u pipewire -u pipewire-pulse \\
    -u a50-headset-manager \\
    --since=-%(window)ds -o short-precise --no-pager
echo "== pw-dump"
timeout -k 1 5 pw-dump 2>&1 || echo "pw-dump failed or timed out"
echo "== Open ALSA streams"
for f in /proc/asound/card*/pcm*/sub0/status; do
    [ -e "$f" ] || continue
    status=$(timeout -k 1 2 cat "$f")
    case $? in
        0) ;;
        124) echo "-- $f: read timed out"; continue ;;
        *) continue ;;
    esac
    [ "${status#closed}" != "$status" ] && continue
    card=$(dirname "$(dirname "$(dirname "$f")")")
    echo "-- $(cat "$card/id") ${f#$card/}"
    echo "$status"
done
echo "== USB audio streams"
for f in /proc/asound/card*/stream*; do
    [ -e "$f" ] || continue
    timeout -k 1 2 cat "$f"
    [ $? = 124 ] && echo "-- $f: read timed out"
done
""" % {"stack": STALL_STACK_TIMEOUT, "window": STALL_LOG_WINDOW}


# The recorder: the last RECORD_LINES lines of the daemon events. These are
# the headset status and the open ALSA streams, routing events and health
# check times. The recorder is in memory only. A stall log starts with it, to
# show the time before the stall.
_recorder = deque(maxlen=RECORD_LINES)


def record(text: str):
    """Add a line with the local date and time to the recorder."""
    now = time.time()
    stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(now))
    _recorder.append(f"{stamp}.{int(now % 1 * 1000):03d} {text}")


# A read of an ALSA status file can wait for a kernel lock without a time
# limit (for example, while a USB audio device hangs). Nothing can stop a
# process in this state until its read ends. For this reason, a background
# thread starts a child process that reads the files. If the child does not
# end in STREAM_READ_TIMEOUT, the thread kills it. A child in state D ends
# only when its read ends. No new child starts until it ends. The main loop
# uses stream_summary(): it gives the last good result, its age, the wait
# time and the last error.
STREAM_READ_TIMEOUT = 2  # seconds
_streams = ("not read yet", time.monotonic())  # last good result and time
_streams_wait = None  # start of a read that did not end in time, or None
_streams_error = None  # the last read error, until a read is good again


def parse_stream_states(output: str) -> str:
    """Make one line from the output of grep -H "" on the ALSA id and status
    files, for example "A50/pcm0p RUNNING hw_ptr=1200 pid=997288". Only the
    first substream (sub0) of each PCM is read."""
    ids = {}
    status = {}
    for line in output.splitlines():
        path, _, value = line.partition(":")
        if path.endswith("/id"):
            ids[os.path.dirname(path)] = value.strip()
        elif path.endswith("/status"):
            status.setdefault(path, []).append(value)
    states = []
    for path in sorted(status):
        text = "\n".join(status[path])
        if text.startswith("closed"):
            continue
        card_dir = path.split("/pcm")[0]
        pcm = path[len(card_dir) + 1:].split("/")[0]
        fields = dict(
            (k.strip(), v.strip()) for k, _, v in
            (line.partition(":") for line in text.splitlines()) if v)
        states.append(f"{ids.get(card_dir, card_dir)}/{pcm} "
                      f"{fields.get('state', '?')} "
                      f"hw_ptr={fields.get('hw_ptr', '?')} "
                      f"pid={fields.get('owner_pid', '?')}")
    return "; ".join(states) or "none"


def _read_streams_forever():
    global _streams, _streams_wait, _streams_error
    child = None
    while True:
        try:
            if child is None or child.poll() is not None:
                _streams_wait = None
                started = time.monotonic()
                paths = (glob.glob("/proc/asound/card*/id")
                         + glob.glob("/proc/asound/card*/pcm*/sub0/status"))
                child = subprocess.Popen(
                    ["grep", "-H", "", *paths],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True,
                    start_new_session=True,
                )
                try:
                    output, _ = child.communicate(timeout=STREAM_READ_TIMEOUT)
                    _streams = (parse_stream_states(output), time.monotonic())
                    _streams_wait = None
                    _streams_error = None
                    child = None
                except subprocess.TimeoutExpired:
                    _streams_wait = started
                    child.kill()
        except Exception as e:
            _streams_error = repr(e)
            _streams_wait = None
        time.sleep(1)


def start_stream_reader():
    """Start the background thread that reads the ALSA streams."""
    threading.Thread(target=_read_streams_forever, daemon=True).start()


def stream_summary() -> str:
    """Return the last good result of the stream reader, with its age. Add
    the time that the current read waits (if a read does not end) and the
    last read error (if the last read failed)."""
    text, when = _streams
    now = time.monotonic()
    summary = f"{text} (age {now - when:.1f} s)"
    wait = _streams_wait
    if wait is not None:
        summary += f"; read waits for {now - wait:.0f} s"
    error = _streams_error
    if error is not None:
        summary += f"; read failed: {error}"
    return summary


def capture_stall_state():
    """Save the recorder and the state of the audio services to a new file in
    STALL_LOG_DIR. Keeps the newest STALL_LOG_KEEP files."""
    try:
        os.makedirs(STALL_LOG_DIR, exist_ok=True)
        old = sorted(glob.glob(os.path.join(STALL_LOG_DIR, "stall-*.txt")))
        for path in old[:max(0, len(old) - STALL_LOG_KEEP + 1)]:
            os.remove(path)
        path = os.path.join(
            STALL_LOG_DIR, time.strftime("stall-%Y%m%d-%H%M%SZ.txt", time.gmtime()))
        with open(path, "w") as out:
            out.write("== Recorder (last lines before the stall)\n")
            out.writelines(line + "\n" for line in _recorder)
        with open(path, "a") as out:
            subprocess.Popen(
                ["sh", "-c", _STALL_CAPTURE_SCRIPT, "sh", *AUDIO_SERVICES],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(f"  Saving audio service state to {path}", flush=True)
    except OSError as e:
        print(f"  Could not save audio service state: {e}", flush=True)


def try_connect_device() -> Device | None:
    """
    Try to connect to the A50 headset dock via USB.

    Handles various failure modes gracefully:
    - DeviceNotConnected: Dock not plugged in
    - USBError: Driver issues or communication errors
    - Other exceptions: Unexpected errors

    Returns a Device instance on success, None on failure.
    Always cleans up properly on failure to avoid leaving USB driver detached.
    """
    device = None
    try:
        device = Device()
        # Test that we can actually communicate with the device
        device.get_headset_status()
        return device
    except DeviceNotConnected:
        # Dock not connected - normal state, no cleanup needed
        return None
    except USBError as e:
        # USB communication error - ensure cleanup
        print(f"USB error during connection: {e}", flush=True)
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        return None
    except Exception as e:
        # Unexpected error - ensure cleanup
        print(f"Unexpected error during connection: {e}", flush=True)
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        return None


def format_node_name(node_name: str) -> str:
    """Format a sink/source name for human-readable display."""
    # Try to extract a meaningful part from the node name
    # e.g., "alsa_output.pci-0000_c3_00.1.HiFi__HDMI1__sink" -> "HDMI1"
    # e.g., "alsa_input.pci-0000_c3_00.6.HiFi__Mic1__source" -> "Mic1"
    if "__" in node_name:
        parts = node_name.split("__")
        if len(parts) >= 2:
            return parts[1]
    # e.g., "alsa_output.pci-0000_00_1f.3.analog-stereo" -> "analog-stereo"
    if "." in node_name:
        return node_name.split(".")[-1]
    return node_name


def main():
    """
    Main monitoring loop with state machine and error recovery.

    States:
    - Disconnected: USB dock not found, waiting with exponential backoff
    - Connected/Docked: Headset on dock, using fallback audio/mic
    - Connected/Active: Headset off dock and powered on, using headset audio/mic

    The loop continuously monitors for:
    - USB dock connection/disconnection
    - Headset dock status changes
    - HDMI hotplug events (periodic fallback re-evaluation)

    It also retries a failed sink or source switch, and repairs a stuck
    audio path (WirePlumber restart or USB reset, then PipeWire restart).
    See the constants above.
    """
    print("A50 Audio Switcher", flush=True)
    start_stream_reader()

    device = None
    last_status = None
    last_fallback_sink = None
    last_fallback_source = None

    # Exponential backoff for reconnection attempts
    backoff_seconds = 2
    max_backoff = 30

    # Counter for periodic fallback re-evaluation (for HDMI hotplug)
    poll_counter = 0
    fallback_check_interval = 10  # Re-check fallback every 10 polls when docked

    # Periodic USB session refresh: defends against stale status reads when the
    # dongle sits behind autosuspending hubs (e.g. the CalDigit TS4 dock, where
    # an upstream hub suspending freezes get_headset_status() on its last value
    # with no error). The udev rule pins hub power; this is the backstop.
    reconnect_counter = 0
    reconnect_interval = 120  # force a fresh USB session every ~120 polls

    # Routing that is not fully applied: "headset", "fallback" or None.
    # The sink and the source are tracked separately, so a retry sets only
    # the half that failed. A half that succeeded is not set again, so a
    # manual choice is not overridden. Retries stop after ROUTE_RETRY_LIMIT.
    route_pending = None
    sink_pending = False
    source_pending = False
    route_retry_delay = ROUTE_RETRY_MIN
    route_next_try = 0.0  # time.monotonic() of the next attempt
    route_deadline = 0.0  # time.monotonic() after which retries stop
    route_hold_until = 0.0  # no routing before this time (after a restart)

    # Audio path health (see HEALTH_CHECK_INTERVAL)
    health_next_check = 0.0  # time.monotonic() of the next health check
    health_failures = 0
    health_fail_since = 0.0  # time.monotonic() of the first failed check
    step1_done = False  # step 1 done in this repair cycle
    step1_time = 0.0  # time.monotonic() of step 1
    last_capture = None  # time.monotonic() of the last stall capture
    last_audio_restart = None  # time.monotonic() of the last PipeWire restart
    restart_cooldown = AUDIO_RESTART_COOLDOWN
    restart_unresolved = False  # no good check since the last restart

    def request_route(target):
        """Mark routing to target (both halves) as pending, due now."""
        nonlocal route_pending, sink_pending, source_pending
        nonlocal route_retry_delay, route_next_try, route_deadline
        record(f"routing to {target} requested" if target is not None
               else "routing cleared")
        route_pending = target
        sink_pending = source_pending = target is not None
        route_retry_delay = ROUTE_RETRY_MIN
        now = time.monotonic()
        route_next_try = now
        route_deadline = now + ROUTE_RETRY_LIMIT

    def switch_fallback_sink() -> bool:
        """Set the best fallback sink. Returns True if one was set."""
        nonlocal last_fallback_sink
        fallback_sink = get_best_fallback_sink()
        if not fallback_sink:
            print("  Output: none available", flush=True)
        elif set_default_sink(fallback_sink):
            print(f"  Output: {format_node_name(fallback_sink)}", flush=True)
            last_fallback_sink = fallback_sink
            return True
        else:
            print("  Output: switch failed", flush=True)
        last_fallback_sink = None
        return False

    def switch_fallback_source() -> bool:
        """Set the best fallback source.

        Returns False only if setting a source failed. Some systems have no
        fallback microphone, so "none available" is not retried; when docked,
        the periodic fallback check sets a microphone that appears later.
        """
        nonlocal last_fallback_source
        fallback_source = get_best_fallback_source()
        if not fallback_source:
            print("  Input: none available", flush=True)
            last_fallback_source = None
            return True
        if set_default_source(fallback_source):
            print(f"  Input: {format_node_name(fallback_source)}", flush=True)
            last_fallback_source = fallback_source
            return True
        print("  Input: switch failed", flush=True)
        last_fallback_source = None
        return False

    def switch_headset_sink() -> bool:
        """Set the A50 sink. Returns True on success."""
        if set_default_sink(HEADSET_SINK):
            return True
        print("  Warning: Could not find A50 Game sink", flush=True)
        return False

    def switch_headset_source() -> bool:
        """Set the A50 source. Returns True on success."""
        if set_default_source(HEADSET_SOURCE):
            return True
        print("  Warning: Could not find A50 Chat source", flush=True)
        return False

    def apply_pending_route(force: bool = False):
        """Set the pending halves of the pending routing, when due.

        Without force, do nothing while the audio path is unhealthy (the
        commands only time out). force=True makes the first attempt for a new
        status even then. A failed attempt doubles the wait and makes the
        next health check due now.
        """
        nonlocal route_pending, sink_pending, source_pending
        nonlocal route_retry_delay, route_next_try, health_next_check
        nonlocal last_fallback_sink, last_fallback_source
        if route_pending is None:
            return
        now = time.monotonic()
        if now < route_hold_until:
            return  # just after a restart; also for a forced switch
        if not force and (health_failures or now < route_next_try):
            return
        if now > route_deadline:
            print(f"  Routing to {route_pending}: retries stopped", flush=True)
            record(f"routing to {route_pending}: retries stopped")
            route_pending = None
            return

        if route_pending == "headset":
            # Clear so we re-evaluate when docked again
            last_fallback_sink = None
            last_fallback_source = None
            if sink_pending:
                sink_pending = not switch_headset_sink()
            if source_pending:
                source_pending = not switch_headset_source()
        else:
            if sink_pending:
                sink_pending = not switch_fallback_sink()
            if source_pending:
                source_pending = not switch_fallback_source()

        if not sink_pending and not source_pending:
            record(f"routing to {route_pending} done")
            route_pending = None
            return
        print(f"  Routing to {route_pending} incomplete; will retry", flush=True)
        record(f"routing to {route_pending} incomplete")
        route_next_try = time.monotonic() + route_retry_delay
        route_retry_delay = min(route_retry_delay * 2, ROUTE_RETRY_MAX)
        health_next_check = 0.0

    def reroute_after_restart():
        """Request the default devices again after a restart. Without a known
        status, use the fallback if the dock is not connected."""
        nonlocal route_next_try, route_hold_until
        if device is None:
            target = "fallback"
        else:
            target = route_pending
            if target is None and last_status is not None:
                target = desired_route(last_status)
        if target is not None:
            request_route(target)
            # Give PipeWire time to list its devices again
            route_hold_until = time.monotonic() + POST_RESTART_DELAY
            route_next_try = route_hold_until

    def check_audio_health():
        """Run a health check when due. Repair the audio path after repeated
        failures."""
        nonlocal device, last_status, health_failures, health_next_check
        nonlocal health_fail_since, step1_done, step1_time, last_capture
        nonlocal last_audio_restart, restart_cooldown, restart_unresolved
        nonlocal route_next_try, route_deadline
        now = time.monotonic()
        if not health_failures and now < health_next_check:
            return
        start = time.monotonic()
        healthy = audio_healthy()
        now = time.monotonic()
        record(f"health check {'passed' if healthy else 'failed'} "
               f"in {now - start:.1f} s")
        if healthy and now - start > HEALTH_SLOW_SECONDS:
            print(f"Slow audio health check: {now - start:.1f} s", flush=True)
        health_next_check = now + HEALTH_CHECK_INTERVAL
        if healthy:
            if health_failures:
                print("Audio path healthy again", flush=True)
                if route_pending is not None:
                    # Retries were paused; give the pending routing a new
                    # retry period, starting now (or after the wait that
                    # follows a restart).
                    route_next_try = max(now, route_hold_until)
                    route_deadline = now + ROUTE_RETRY_LIMIT
            health_failures = 0
            step1_done = False
            restart_cooldown = AUDIO_RESTART_COOLDOWN
            restart_unresolved = False
            return

        if not health_failures:
            health_fail_since = now
        health_failures += 1
        print(f"Audio health check failed ({health_failures})", flush=True)
        failing_for = now - health_fail_since

        if health_failures == 2 and (
                last_capture is None
                or now - last_capture >= STALL_CAPTURE_INTERVAL):
            last_capture = now
            capture_stall_state()

        if (not step1_done and health_failures >= HEALTH_FAILURE_LIMIT
                and failing_for >= REPAIR_MIN_SECONDS):
            step1_done = True
            if dock_usb_errors():
                # Step 1a: USB reset of the base station. Close our session
                # first; the main loop opens a new one and routes audio again.
                print("Repair: USB reset of the base station", flush=True)
                if device is not None:
                    try:
                        device.close()
                    except Exception:
                        pass
                    device = None
                last_status = None
                reset_dock_usb()
            else:
                # Step 1b: restart WirePlumber only. Then set the default
                # devices again (WirePlumber can restore an older default).
                print("Repair: restart of WirePlumber", flush=True)
                if restart_audio_services([SESSION_MANAGER]):
                    print("  WirePlumber restarted", flush=True)
                reroute_after_restart()
            # Measure the wait for step 2 from the end of step 1 (the repair
            # can take up to 30 s)
            step1_time = time.monotonic()

        elif (step1_done and health_failures >= 2 * HEALTH_FAILURE_LIMIT
                and failing_for >= 2 * REPAIR_MIN_SECONDS
                and now - step1_time >= REPAIR_MIN_SECONDS):
            # Step 2: restart PipeWire, at most once per cooldown period
            if (last_audio_restart is not None
                    and now - last_audio_restart < restart_cooldown):
                return
            if restart_unresolved:
                # The last restart did not fix the fault: wait longer
                restart_cooldown = min(restart_cooldown * 2, AUDIO_RESTART_COOLDOWN_MAX)
            print("Repair: restart of the PipeWire services", flush=True)
            last_audio_restart = now
            restart_unresolved = True
            if restart_audio_services():
                # Apps that do not reconnect by themselves (e.g. Spotify)
                # must be restarted by the user.
                print("  PipeWire services restarted", flush=True)
            # Start a new repair cycle, also when systemctl failed or timed
            # out (systemd can still finish the restart).
            health_failures = 0
            step1_done = False
            reroute_after_restart()

    def desired_route(status):
        """Return the routing target for a headset status, or None."""
        if status.is_on and not status.is_docked:
            return "headset"
        if status.is_docked:
            return "fallback"
        return None

    while True:
        # === STATE: Disconnected ===
        # Try to connect to USB dock if not connected
        if device is None:
            device = try_connect_device()
            if not device:
                record(f"no dock; streams: {stream_summary()}")
            if device:
                print("Dock connected", flush=True)
                # Reset backoff on successful connection
                backoff_seconds = 2
                last_status = None  # Reset to trigger state update
            else:
                # Without the dock, pending routing and health checks still
                # run, so a stuck PipeWire is repaired.
                apply_pending_route()
                check_audio_health()
                # Wait with exponential backoff before retry. While a retry
                # or a repair is in progress, wait at most BUSY_POLL_MAX.
                if route_pending is not None or health_failures:
                    time.sleep(min(backoff_seconds, BUSY_POLL_MAX))
                else:
                    time.sleep(backoff_seconds)
                # Increase backoff for next attempt (capped at max)
                backoff_seconds = min(backoff_seconds * 2, max_backoff)
                continue

        # === Periodic USB session refresh (defensive against stale reads) ===
        # Re-open the USB session every reconnect_interval polls so a frozen
        # status endpoint (from an upstream hub autosuspending) can't silently
        # stall dock/undock detection. last_status is deliberately preserved so
        # a refresh never re-asserts audio routing over a manual sink choice.
        reconnect_counter += 1
        if reconnect_counter >= reconnect_interval:
            reconnect_counter = 0
            try:
                device.close()
            except Exception:
                pass
            device = try_connect_device()
            if device is None:
                print("Periodic refresh: dock unreachable, retrying", flush=True)
                record("periodic refresh: dock unreachable")
                last_status = None
                time.sleep(backoff_seconds)
                continue

        # === STATE: Connected ===
        # Try to get headset status, handle disconnect
        try:
            status = device.get_headset_status()
        except (USBError, DeviceNotConnected) as e:
            # USB dock disconnected or communication error
            print(f"Dock disconnected ({type(e).__name__})", flush=True)
            record(f"dock disconnected ({type(e).__name__})")
            # Clean up device and reattach kernel driver
            try:
                device.close()
            except Exception:
                pass
            device = None
            last_status = None

            # Switch to fallback audio on disconnect. This replaces any
            # older pending routing.
            print("Switching to fallback:", flush=True)
            request_route("fallback")
            apply_pending_route(force=True)

            time.sleep(backoff_seconds)
            continue
        except Exception as e:
            # Unexpected error - also disconnect and retry
            print(f"Unexpected error: {e}", flush=True)
            record(f"unexpected error: {e}")
            try:
                device.close()
            except Exception:
                pass
            device = None
            last_status = None
            time.sleep(backoff_seconds)
            continue

        record(f"status on={status.is_on} docked={status.is_docked}; "
               f"streams: {stream_summary()}")

        # === Handle headset status changes ===
        poll_counter += 1

        if status != last_status:
            target = desired_route(status)
            if target == "headset":
                print("Headset active - switching to A50", flush=True)
            elif target == "fallback":
                print("Headset docked - switching to fallback:", flush=True)
            # Log the raw status bits. This helps to find false readings.
            print(f"  Status: on={status.is_on} docked={status.is_docked}",
                  flush=True)
            # A new status replaces any older pending routing. The first
            # attempt runs even while the audio path is unhealthy.
            request_route(target)
            apply_pending_route(force=True)
            last_status = status
            poll_counter = 0  # Reset counter on status change

        elif (route_pending is None and not health_failures and status.is_docked
                and poll_counter >= fallback_check_interval):
            # Periodic re-evaluation of fallback devices (for HDMI hotplug detection)
            # This catches cases where a monitor is plugged/unplugged while headset is docked
            poll_counter = 0

            fallback_sink = get_best_fallback_sink()
            fallback_source = get_best_fallback_source()

            if fallback_sink != last_fallback_sink or fallback_source != last_fallback_source:
                print("Fallback changed:", flush=True)
                if fallback_sink != last_fallback_sink:
                    if fallback_sink:
                        print(f"  Output: {format_node_name(fallback_sink)}", flush=True)
                        if set_default_sink(fallback_sink):
                            last_fallback_sink = fallback_sink
                    else:
                        print("  Output: none available", flush=True)
                        last_fallback_sink = None

                if fallback_source != last_fallback_source:
                    if fallback_source:
                        print(f"  Input: {format_node_name(fallback_source)}", flush=True)
                        if set_default_source(fallback_source):
                            last_fallback_source = fallback_source
                    else:
                        print("  Input: none available", flush=True)
                        last_fallback_source = None

        # === Retry pending routing ===
        apply_pending_route()

        # === Audio path health check ===
        check_audio_health()
        if device is None:
            continue  # USB reset done; reconnect on the next pass

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting", flush=True)
