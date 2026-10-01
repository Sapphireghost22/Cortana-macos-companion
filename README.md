# Local Mac Assistant: architecture and one-hour proof of concept

## Design intentx

Build a private, local-first desktop assistant with an original personality: dryly funny, loyal, observant, empathetic, and technically sharp. Use a cool blue, glass-like interface with restrained waveform and diagnostic animations. The voice direction should be calm, precise, and lightly wry; do not clone a particular actor or character voice.

Keep telemetry, speech recognition, language-model inference, and conversation history on the Mac. The Wi-Fi probe sends only local gateway pings. Any future internet-capable feature should be explicit and separate.

## Architecture

~~~text
┌──────────────────────────────────────────────────────────────┐
│ UI + Persona                                                 │
│ SwiftUI menu-bar panel · transcript · waveform · voice       │
│ state machine · alert policy · local intent/LLM response     │
└──────────────────────────────┬───────────────────────────────┘
                               │ typed events / actions
┌──────────────────────────────▼───────────────────────────────┐
│ Local assistant core                                         │
│ event bus · threshold/hysteresis rules · reminder scheduler  │
│ local search adapter (Spotlight/mdfind) · local model router │
│ action allowlist + user confirmation for state-changing ops  │
└──────────────┬─────────────────┬─────────────────┬───────────┘
               │                 │                 │
┌──────────────▼──────┐ ┌────────▼────────┐ ┌──────▼───────────┐
│ Wi-Fi bridge        │ │ Bluetooth bridge│ │ System bridge    │
│ CoreWLAN RSSI/scan  │ │ CoreBluetooth / │ │ ProcessInfo      │
│ gateway ping        │ │ Bleak scan      │ │ pmset, vm_stat   │
└─────────────────────┘ └─────────────────┘ └──────────────────┘

Voice: local STT -> intent router -> local LLM or rules -> macOS say / local TTS
~~~

Suggested code layout:

~~~text
local-mac-assistant/
  app/                 SwiftUI menu bar shell and permissions
  core/                event bus, policy, persona state machine
  bridges/             Wi-Fi, Bluetooth, battery, thermal, Spotlight
  voice/               on-device STT/TTS adapters
  config/              named devices, thresholds, quiet hours
  scripts/             this proof-of-concept bundle
~~~

Bridges publish timestamped observations; they do not make decisions or speak. The core applies thresholds and hysteresis, then asks the persona layer to phrase a fact-based message. Keep commands that change Wi-Fi, Bluetooth, or files behind an explicit user action.

## Utility scripts

### Requirements

- macOS on Apple Silicon or Intel.
- Python 3.9 or newer.
- Swift command-line tools for CoreWLAN and thermal-state queries. Xcode Command Line Tools provide Swift on Macs where it is not already available.
- Bluetooth scan: install Bleak with: python3 -m pip install bleak. Bleak uses the macOS CoreBluetooth backend.

These scripts are diagnostic snapshots, not a privileged daemon. They do not install launch agents, change radios, or retain telemetry.

### Wi-Fi snapshot

Run python3 wifi_status.py for the active connection and local gateway probe. Add --scan to request one nearby Wi-Fi scan. CoreWLAN is the supported API used for signal and scan data; the script avoids relying on the legacy airport command.

### Bluetooth scan

Run python3 bluetooth_scan.py --seconds 8 --known "My Phone" --known "My AirPods". It reports currently advertising BLE peripherals and any battery values macOS itself exposes in its Bluetooth system report. Add --read-gatt-battery to try the standard GATT Battery Service on discovered peripherals that support it.

BLE visibility is not a dependable distance sensor. A paired phone or AirPods may not advertise while connected, asleep, or using private identifiers. RSSI varies with body position and radio conditions; use a time window and hysteresis, and label the result “nearby signal seen,” not a distance.

### System snapshot

Run python3 system_health.py. It reports battery percentage and cycle count when macOS exposes them, a public thermal state, an approximate CPU utilization estimate, and RAM use from virtual-memory counters. Thermal state is not a temperature reading. Missing cycle counts or accessory battery values are returned as unavailable rather than guessed.

## Persona state machine

States: IDLE, LISTENING, THINKING, RESPONDING, ALERT, and COOLING.

1. Sensor bridges publish facts such as battery_pct=18, thermal_state=serious, gateway_loss_pct=25, or a named peripheral advertisement.
2. A policy layer validates freshness, applies thresholds, then requires a second sample or a sustained interval before raising an alert.
3. Hysteresis clears an alert only after recovery crosses a different threshold. Cooldowns suppress repeated narration.
4. The persona layer selects a short line from a state-specific template, optionally lets a local model add phrasing, then checks the wording against the structured facts. It never invents a sensor reading.
5. The UI displays every alert. Speech follows quiet hours and user volume settings; critical system conditions reduce animation and pause optional local inference.

Starter thresholds: low battery at 20% on battery power (clear above 25%); serious thermal state immediately (clear at fair or nominal after 2 minutes); gateway loss above 20% over 3 probes (clear below 5% for 2 snapshots). Make thresholds configurable.

Example original lines:

- Low battery: “We’re at 18%, and the charger is conspicuously absent. I can dim the theatrics and save some power.”
- Thermal serious: “Thermals are elevated. I’m pausing the unnecessary cleverness so the Mac can shed some heat.”
- Network loss: “The gateway missed two of three checks. The local network path is having a day.”
- Recovery: “Thermal state is back in the comfortable range. Resuming the low-priority diagnostics.”

## One-hour implementation roadmap

1. **0–10 min:** Create a Python virtual environment, install Bleak, and run the three snapshots once. Grant Bluetooth access when macOS prompts. Verify unavailable fields are displayed as unavailable.
2. **10–20 min:** Build a minimal SwiftUI menu-bar panel with a status label, transcript area, and a blue radial/waveform animation. Add a clear microphone toggle and Bluetooth purpose string in the app Info.plist.
3. **20–30 min:** Poll system and Wi-Fi bridges every 30–60 seconds; run Bluetooth scans only on demand or at a slower configurable interval. Convert their JSON output to typed events.
4. **30–40 min:** Implement threshold, debounce, hysteresis, cooldown, quiet-hours, and thermal-degraded modes. Start with deterministic rules and the sample lines above.
5. **40–50 min:** Add local TTS with the installed macOS say voice. Add local STT only if time permits; never fall back to a remote recognizer. Whisper.cpp or Apple Speech with on-device recognition can be evaluated later for the selected language and hardware.
6. **50–60 min:** Try unplugged, low-battery, and disconnected-network scenarios; check that alerts do not repeat every poll. Save configuration locally and confirm the assistant still works with internet access disabled.

The one-hour target is a visible, rule-based local prototype. Full-time background behavior, polished permissions, robust device identity, and model tuning are follow-on work.
