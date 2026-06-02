# Routing Reachy Mini audio through a Bluetooth headset

How to make the conversation app's audio (speaker + optionally mic) come out of a
Bluetooth headset instead of the built-in XVF3800, on the robot (Raspberry Pi
CM4 running Linux with PipeWire + WirePlumber + BlueZ).

Two independent problems have to be solved, **in order**:

1. **Bluetooth audio doesn't work at all on the robot** — WirePlumber never
   registers A2DP/HFP endpoints, so `connect` fails with
   `br-connection-profile-unavailable`. → Fix in §1.
2. **The app bypasses PipeWire** — the SDK writes straight to ALSA `hw:0,0`
   (the XVF3800), so even a working BT device is ignored. → Fix in §2.

> All steps run **on the robot** (`ssh pollen@reachy-mini`). Nothing here changes
> the conversation app's code.

---

## Background: how the app picks an audio device

- The app never selects a device. It calls `robot.media.push_audio_sample()` /
  `get_audio_sample()` (`console.py`), delegating everything to the
  `reachy_mini` SDK's `MediaManager`.
- In the `LOCAL` backend the SDK opens audio by the **ALSA PCM names**
  `reachymini_audio_sink` / `reachymini_audio_src`, because
  `~/.asoundrc` defines them (`has_reachymini_asoundrc()` returns true).
- On this robot `~/.asoundrc` routes those names to raw **`hw:0,0`** (the
  "Reachy Mini Audio" USB card = XVF3800), which **bypasses PipeWire** and
  therefore any Bluetooth device.
- Bluetooth audio lives only inside PipeWire, so the two never meet until we
  repoint those ALSA names into PipeWire (§2).

---

## §1 — Make Bluetooth audio work on the robot (one-time)

### Root cause

WirePlumber's BlueZ monitor (`/usr/share/wireplumber/scripts/monitors/bluez.lua`)
only registers A2DP/HFP endpoints when the **logind seat is `"active"`**:

```lua
if config.seat_monitoring then
  logind_plugin = Plugin.find("logind")
end
if logind_plugin then
  function startStopMonitor(seat_state)
    if seat_state == "active" then
      monitor = createMonitor()      -- endpoints registered ONLY here
    elseif monitor then ... end
  end
  startStopMonitor(logind_plugin:call("get-state"))
else
  monitor = createMonitor()          -- no seat gating → always on
end
```

The active WirePlumber profile is `main`, which inherits only `base` — it does
**not** inherit `mixin.systemwide-session`, the only block that sets
`monitor.bluez.seat-monitoring = disabled`. So seat-gating is **on**.

On a headless robot all sessions are **seatless** (`loginctl` shows `Seat=` empty,
`Remote=yes`), so logind reports `"online"`, never `"active"`. Result:
`createMonitor()` is never called → no audio endpoints on the adapter →
`bluetoothctl connect` fails with `org.bluez.Error.Failed
br-connection-profile-unavailable`.

Confirmed not-the-cause: headset is fine (paired, advertises A2DP + HFP);
`libspa-0.2-bluetooth` is installed; `org.bluez.Media1` is present and supports
A2DP; the D-Bus default policy allows endpoint registration (the `bluetooth`
group is not required).

### Fix: disable seat-monitoring for the BlueZ monitor

Add a WirePlumber drop-in so the `main` profile takes the always-on branch.
This is user-space config and survives reboot:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cat > ~/.config/wireplumber/wireplumber.conf.d/50-bluez-no-seat.conf <<'EOF'
# Headless robot: no active graphical seat, so disable seat-gating for the
# BlueZ monitor — otherwise it never starts and no A2DP/HFP endpoints register.
wireplumber.profiles = {
  main = {
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF

systemctl --user restart wireplumber
sleep 2
```

The merge is key-wise: `hardware.bluetooth = required` and the rest of `main`
stay intact; you only add the one flag.

> Alternative: `support.logind = disabled` in the same `main` block has the same
> effect more bluntly (`Plugin.find("logind")` returns nil → always-on branch).
> Disabling just seat-monitoring is the targeted choice.

### Verify

```bash
bluetoothctl show | grep -i UUID
#   expect to NOW see: Audio Source, Audio Sink, Handsfree Audio Gateway
```

If those audio UUIDs are absent, capture why:

```bash
systemctl --user stop wireplumber
timeout 8 env WIREPLUMBER_DEBUG=D wireplumber > /tmp/wp.log 2>&1
systemctl --user start wireplumber
grep -iE "bluez5|bluez\.lua|adapter|RegisterEndpoint|not supported|broken" /tmp/wp.log \
  | grep -viE "pw_conf_find_match|map factory|pw\.conf"
```

---

## §2 — Pair the headset and route the app to it

### Pair (first time only, interactive)

```bash
bluetoothctl
power on
agent on
default-agent
scan on                           # wait for the headset MAC to appear
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF         # works only after §1
scan off
exit
```

### Profile trade-off (a Bluetooth limitation, not the robot's)

| Profile | Output | Mic | Quality |
|---|---|---|---|
| **HFP/HSP** (`headset-head-unit`) | yes | **yes** | mono, ~telephone |
| **A2DP** (`a2dp-sink`)            | yes | **no**  | hi-fi stereo |

You cannot have hi-fi output **and** the BT mic at the same time.

### `bt-audio.sh` — apply / revert the routing

Why it works: the SDK addresses audio by the ALSA PCM *names*
`reachymini_audio_sink` / `reachymini_audio_src`. Repointing those names to
`type pulse` sends the app's audio into PipeWire, which then routes to the
current default sink/source (the headset). The names stay present, so
`has_reachymini_asoundrc()` stays true.

```bash
#!/usr/bin/env bash
#
# bt-audio.sh — route the Reachy Mini conversation app's audio through a
# Bluetooth headset (or revert to the built-in XVF3800).
#
# Usage:
#   ./bt-audio.sh apply  AA:BB:CC:DD:EE:FF [hfp|a2dp]   # default profile: hfp
#   ./bt-audio.sh revert
#
#   hfp  = mic + speaker together (mono, ~telephone quality)
#   a2dp = high-quality output only, NO bluetooth mic (keeps ReSpeaker mic)
#
set -euo pipefail

ASOUNDRC="$HOME/.asoundrc"
ORIG="$HOME/.asoundrc.orig"        # preserved true original (first run only)

die()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo ">>> $*"; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }

revert() {
  [ -f "$ORIG" ] || die "no backup found at $ORIG — nothing to revert."
  cp -f "$ORIG" "$ASOUNDRC"
  info "Restored original $ASOUNDRC from $ORIG"
  info "Now restart the conversation app for it to reopen the audio devices."
}

apply() {
  local mac="${1:-}" mode="${2:-hfp}"
  [ -n "$mac" ] || die "usage: $0 apply AA:BB:CC:DD:EE:FF [hfp|a2dp]"
  case "$mode" in hfp|a2dp) ;; *) die "mode must be 'hfp' or 'a2dp'";; esac

  need bluetoothctl; need pactl; need wpctl; need awk

  local mac_us; mac_us="${mac//:/_}"
  local card="bluez_card.${mac_us}"
  local profile;  [ "$mode" = hfp ] && profile="headset-head-unit" || profile="a2dp-sink"

  # 1. Connect (assumes already paired+trusted once via bluetoothctl).
  info "Connecting $mac ..."
  bluetoothctl connect "$mac" || die "connect failed — pair/trust it first: \
bluetoothctl -> scan on -> pair $mac -> trust $mac -> connect $mac"

  # 2. Wait for PipeWire/BlueZ to expose the card.
  info "Waiting for $card to appear in PipeWire ..."
  for i in $(seq 1 20); do
    pactl list cards short | grep -q "$card" && break
    sleep 0.5
    [ "$i" = 20 ] && die "bluez card $card never appeared (check 'wpctl status')."
  done

  # 3. Select the profile (HFP = mic+speaker, A2DP = output only).
  info "Available profiles for $card:"
  pactl list cards | awk -v c="$card" '
    $0 ~ "Name: "c {f=1} f&&/Profiles:/{p=1;next} p&&/^\t\t[a-z]/{print "      "$0}
    f&&/Active Profile/{print "    "$0; f=0; p=0}'
  info "Setting profile -> $profile"
  pactl set-card-profile "$card" "$profile" \
    || die "could not set profile '$profile' (see the list above for valid names)."
  sleep 1

  # 4. Find resulting sink (and source, for hfp) node names and set defaults.
  local sink src
  sink="$(pactl list short sinks   | awk -v m="$mac_us" '$2 ~ "bluez" && $2 ~ m {print $2; exit}')"
  [ -n "$sink" ] || die "no bluez sink found after profile switch."
  info "Default sink -> $sink"
  pactl set-default-sink "$sink"

  if [ "$mode" = hfp ]; then
    src="$(pactl list short sources | awk -v m="$mac_us" '$2 ~ "bluez" && $2 ~ m && $2 !~ "monitor" {print $2; exit}')"
    [ -n "$src" ] || die "no bluez source found (HFP profile may not have a mic)."
    info "Default source -> $src"
    pactl set-default-source "$src"
  else
    info "A2DP selected: leaving the microphone on the built-in ReSpeaker."
  fi

  # 5. Back up the true original (first run only), then repoint the SDK's PCMs.
  [ -f "$ASOUNDRC" ] && [ ! -f "$ORIG" ] && cp -f "$ASOUNDRC" "$ORIG" && info "Saved original -> $ORIG"

  cat > "$ASOUNDRC" <<'AEOF'
# Patched by bt-audio.sh: route the Reachy SDK's named PCMs through PipeWire
# (which then sends audio to the current default sink/source — e.g. Bluetooth).
# Revert with: ./bt-audio.sh revert

pcm.!default { type hw
    card 0
}
ctl.!default { type hw
    card 0
}

pcm.reachymini_audio_sink { type pulse }
ctl.reachymini_audio_sink { type pulse }
pcm.reachymini_audio_src  { type pulse }
ctl.reachymini_audio_src  { type pulse }
AEOF
  info "Rewrote $ASOUNDRC to route reachymini_audio_sink/src -> PipeWire."

  echo
  info "DONE. Now RESTART the conversation app so the SDK reopens the devices."
  info "Verify while it runs:  pactl list short sink-inputs   (app should be on the bluez sink)"
  info "To undo everything:  $0 revert   (then restart the app again)"
}

cmd="${1:-}"; shift || true
case "$cmd" in
  apply)  apply "$@";;
  revert) revert;;
  *) die "usage: $0 {apply AA:BB:CC:DD:EE:FF [hfp|a2dp] | revert}";;
esac
```

### Run it

```bash
chmod +x bt-audio.sh
./bt-audio.sh apply AA:BB:CC:DD:EE:FF hfp    # mic + speaker
# or
./bt-audio.sh apply AA:BB:CC:DD:EE:FF a2dp   # hi-fi output only, keep ReSpeaker mic
```

Then **restart the conversation app** (from the Reachy dashboard / however you
launch it) so the SDK reopens `reachymini_audio_sink/src`.

### Revert

```bash
./bt-audio.sh revert      # restores ~/.asoundrc.orig
# then restart the conversation app
```

---

## Caveats

- **Echo cancellation / beamforming**: routing to a headset bypasses the
  XVF3800's hardware AEC, noise suppression, and the app's `startup_config`
  tuning. With a headset (earpiece + close mic) echo is usually a non-issue.
- **Latency**: Bluetooth adds ~100–300 ms (HFP often more), which can desync the
  head-wobble/lip movement from the audio.
- **No hi-fi + mic**: A2DP = output only; HFP = both but mono/low-fi. Bluetooth
  profile limitation, not fixable here.
- **Scope**: this affects the headless `LOCAL` audio path. The Gradio/simulation
  path already streams audio over WebRTC.

## Alternative

If Bluetooth proves too laggy/lossy, route the app's audio to a browser over the
network (WebSocket PCM → Web Audio API) and use any headset on that machine —
sidesteps BlueZ/profile constraints entirely. See the browser-audio proposal.

---

## Quick reference — verification commands

```bash
bluetoothctl show | grep -i UUID                 # adapter audio profiles present?
bluetoothctl info AA:BB:CC:DD:EE:FF | grep Connected
wpctl status                                     # BT sink/source present?
pactl list short sink-inputs                     # app routed to bluez sink?
```
