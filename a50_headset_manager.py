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

import re
import subprocess
import time
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


def run_audio_cmd(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess | None:
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
            timeout=SUBPROCESS_TIMEOUT, check=check,
        )
    except subprocess.TimeoutExpired:
        print(f"Timeout after {SUBPROCESS_TIMEOUT}s: {' '.join(cmd)}", flush=True)
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
    sinks = [s for s in sinks if s.name != HEADSET_SINK]

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
    """
    print("A50 Audio Switcher", flush=True)

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

    def switch_to_fallback():
        """Switch both sink and source to fallback devices."""
        nonlocal last_fallback_sink, last_fallback_source

        # Switch output (sink)
        fallback_sink = get_best_fallback_sink()
        if fallback_sink:
            print(f"  Output: {format_node_name(fallback_sink)}", flush=True)
            set_default_sink(fallback_sink)
            last_fallback_sink = fallback_sink
        else:
            print("  Output: none available", flush=True)
            last_fallback_sink = None

        # Switch input (source/microphone)
        fallback_source = get_best_fallback_source()
        if fallback_source:
            print(f"  Input: {format_node_name(fallback_source)}", flush=True)
            set_default_source(fallback_source)
            last_fallback_source = fallback_source
        else:
            print("  Input: none available", flush=True)
            last_fallback_source = None

    while True:
        # === STATE: Disconnected ===
        # Try to connect to USB dock if not connected
        if device is None:
            device = try_connect_device()
            if device:
                print("Dock connected", flush=True)
                # Reset backoff on successful connection
                backoff_seconds = 2
                last_status = None  # Reset to trigger state update
            else:
                # Wait with exponential backoff before retry
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
            # Clean up device and reattach kernel driver
            try:
                device.close()
            except Exception:
                pass
            device = None
            last_status = None

            # Switch to fallback audio on disconnect
            print("Switching to fallback:", flush=True)
            switch_to_fallback()

            time.sleep(backoff_seconds)
            continue
        except Exception as e:
            # Unexpected error - also disconnect and retry
            print(f"Unexpected error: {e}", flush=True)
            try:
                device.close()
            except Exception:
                pass
            device = None
            last_status = None
            time.sleep(backoff_seconds)
            continue

        # === Handle headset status changes ===
        poll_counter += 1

        if status != last_status:
            if status.is_on and not status.is_docked:
                # Headset is being worn - switch to A50 audio
                print("Headset active - switching to A50", flush=True)
                if not set_default_sink(HEADSET_SINK):
                    print("  Warning: Could not find A50 Game sink", flush=True)
                if not set_default_source(HEADSET_SOURCE):
                    print("  Warning: Could not find A50 Chat source", flush=True)
                # Clear so we re-evaluate when docked again
                last_fallback_sink = None
                last_fallback_source = None

            elif status.is_docked:
                # Headset is on dock - switch to fallback audio
                print("Headset docked - switching to fallback:", flush=True)
                switch_to_fallback()

            last_status = status
            poll_counter = 0  # Reset counter on status change

        elif status.is_docked and poll_counter >= fallback_check_interval:
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
                        set_default_sink(fallback_sink)
                        last_fallback_sink = fallback_sink
                    else:
                        print("  Output: none available", flush=True)
                        last_fallback_sink = None

                if fallback_source != last_fallback_source:
                    if fallback_source:
                        print(f"  Input: {format_node_name(fallback_source)}", flush=True)
                        set_default_source(fallback_source)
                        last_fallback_source = fallback_source
                    else:
                        print("  Input: none available", flush=True)
                        last_fallback_source = None

        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting", flush=True)
