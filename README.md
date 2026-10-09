# a50-headset-manager

Automatic audio input/output switching daemon for the Astro A50 wireless headset
(generation 4).

This daemon monitors your A50 headset status and automatically switches your
system's default audio output (speakers/headphones) and input (microphone):

- **Headset active** (off dock, powered on): Routes audio output to the A50 headset
  and sets the A50 microphone as the default input
- **Headset docked/disconnected**: Falls back to HDMI output (if monitor connected)
  or internal speakers, and switches the microphone to the internal mic array

## Tested Configuration

This daemon has only been tested on:
- **Hardware:** Framework 16 laptop
- **OS:** Arch Linux
- **Audio:** PipeWire with WirePlumber

It may work on other Linux configurations but is not guaranteed.

## Requirements

- Linux with PipeWire/PulseAudio
- Python 3.10+
- Astro A50 Gen 4 headset and base station

## Installation

### Using pipx (recommended)

```bash
pipx install git+https://github.com/gmg-catapultam/a50-headset-manager.git
```

### From source

```bash
git clone https://github.com/gmg-catapultam/a50-headset-manager.git
cd a50-headset-manager
pipx install .
```

## USB Access (required)

Create a udev rule to allow non-root access to the A50 base station:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="9886", ATTR{idProduct}=="002c", MODE:="0666"' | \
    sudo tee /etc/udev/rules.d/50-astro-a50.rules
```

Re-plug your base station to apply the rule.

## Running as a systemd service

Copy the service file to your systemd user directory:

```bash
mkdir -p ~/.config/systemd/user
cp a50-headset-manager.service ~/.config/systemd/user/
```

Or if installed via pipx/pip, download the service file:

```bash
mkdir -p ~/.config/systemd/user
curl -o ~/.config/systemd/user/a50-headset-manager.service \
    https://raw.githubusercontent.com/gmg-catapultam/a50-headset-manager/main/a50-headset-manager.service
```

Enable and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable a50-headset-manager
systemctl --user start a50-headset-manager
```

Check status:

```bash
systemctl --user status a50-headset-manager
```

View logs:

```bash
journalctl --user -u a50-headset-manager -f
```

## Running manually

```bash
a50-headset-manager
```

## Audio Priority

### Output (speakers/headphones)
1. A50 headset (when active)
2. HDMI output (when monitor with audio is connected)
3. Internal speakers

### Input (microphone)
1. A50 headset microphone (when active)
2. Internal microphone array
3. External microphone input

## Recovery

If a switch fails (for example, PipeWire is slow or the output is not
available yet), the daemon tries again. The output and the input are
retried separately, so only the part that failed is set again. The first
retry is after 2 seconds, and the wait doubles after each failure, up to
60 seconds. After 10 minutes, the daemon stops the retries. While PipeWire
does not answer, retries pause; when it answers again, a new 10-minute
retry period starts. (When the
headset is docked, the periodic fallback check still tries to set a
fallback output that changed.) If no fallback
microphone exists, the daemon does not retry the input; when the headset is
docked, the periodic fallback check sets a microphone that appears later.

After a switch succeeds, the daemon does not set it again until one of
these events occurs, so a manual choice stays until then:

- the headset state changes, or the dock is disconnected
- the base station is reset, or the USB session cannot be opened again
- WirePlumber or PipeWire is restarted
- the best fallback output or microphone changes (for example, a monitor
  is connected) while the headset is docked

The daemon also checks that PipeWire answers (`pactl list short sinks`
and `pw-cli ls Node`, 5-second limit each). It checks every 10 seconds, at once after a failed
switch, and every poll while checks fail. When checks fail:

1. At the second failed check, it saves a stall log to
   `~/.local/state/a50-headset-manager/stall-*.txt` (at most once in 10
   minutes; the newest 20 files are kept; file names use UTC). These files
   help to find the cause of a stall. A stall log contains:
   - The recorder: the last 300 lines of daemon events (about 5 minutes
     while the dock is connected). These are the headset status, the open
     ALSA streams (state, position and owner process, from
     `/proc/asound`), routing events and health check times. The daemon
     keeps the recorder in memory. A background thread starts a child
     process that reads the ALSA streams. If the child does not end in
     2 seconds, the thread kills it. No new child starts until it ends.
     For this reason, a read that waits for a kernel lock does not stop
     the daemon. Each status line shows the last good stream data and its
     age. If a read does not end, the line also shows how long the read
     waits. If a read failed, the line also shows the error.
   - The threads (kernel wait point, state, CPU time) of the
     `wireplumber`, `pipewire` and `pipewire-pulse` services, two times
     with 1 second between them, and their stacks. The stacks need
     `eu-stack` (elfutils). `eu-stack` stops the threads of each service
     for a short time. If a thread of a service waits in the kernel
     (state D), the daemon saves no stacks for that service.
   - The last 3 minutes of the kernel log (without "split lock" lines)
     and of the audio service and daemon logs.
   - The `pw-dump` output (if PipeWire answers in 5 seconds).
   - The open ALSA streams and the USB audio streams at the time of the
     stall. These are last: if a read waits for a kernel lock, only this
     part is lost.

   A stall log is about 0.5 MB, most of it from `pw-dump`.

   A health check that passes but takes more than 2 seconds is written to
   the log as "Slow audio health check".

   For more data, make PipeWire log ALSA stream and node state changes.
   This adds few lines to the journal. Put this in
   `~/.config/systemd/user/pipewire.service.d/50-log-topics.conf`:

   ```ini
   [Service]
   Environment=PIPEWIRE_DEBUG=2,spa.alsa:3,pw.node:3
   ```

   Then run `systemctl --user daemon-reload`. The setting starts at the
   next PipeWire start. To start it now without a restart, run
   `pw-metadata -n settings 0 log.level '2,spa.alsa:3,pw.node:3'`.
2. After 3 failed checks in sequence, and at least 15 seconds of
   failures:
   - If the kernel logged a USB error for the base station in the last
     60 seconds, it does a USB reset of the base station. Then it sets
     the devices for the headset state again. To read the kernel log,
     the user must be in the `wheel`, `adm` or `systemd-journal` group.
     If the user cannot read it, the daemon never does the USB reset. The
     log then shows "Kernel log:" and the error.
   - If not, it restarts only the `wireplumber` user service, and then
     sets the default devices again after 5 seconds. Apps stay connected. Playback can
     stop for a short time.
3. After at least 6 failed checks in total, at least 30 seconds of
   failures, and at least 15 seconds after step 2, it restarts the
   `wireplumber`, `pipewire` and `pipewire-pulse` user services. After 5 seconds, it sets the default devices again (the
   fallback devices if the dock is not connected).
4. If the fault stays, these steps repeat. The minimum time between
   restarts is 10 minutes, and each later wait is twice the one before
   (20 minutes, 40 minutes), up to 1 hour. A good check sets it back to
   10 minutes.

While PipeWire does not answer, each poll can take up to about 12
seconds, and up to about 45 seconds during a restart. A switch in the
same poll adds up to about 30 seconds. The daemon detects dock changes more slowly during this time.

**Note:** some apps (for example, Spotify) do not reconnect after a
PipeWire restart. Restart these apps to get sound again.

## Acknowledgments

Uses [eh-fifty](https://github.com/tdryer/eh-fifty) by Tom Dryer, a Python
library for configuring the Astro A50 headset (MIT licensed).

## License

MIT License - see [LICENSE](LICENSE) for details.
