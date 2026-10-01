#!/usr/bin/env python3
"""Run one diagnostics snapshot or a low-frequency local monitoring loop.

Made with [Chat Gpt Luna]
"""

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hardware_bridge import MacHardwareBridge
from persona_engine import CortanaPersonaEngine


def load_settings(path, example_path):
    source = path if path.is_file() else example_path
    try:
        settings = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Could not read {}: {}".format(source, error)) from error
    if not isinstance(settings, dict):
        raise ValueError("the settings file must contain a JSON object")
    if not isinstance(settings.get("known_devices", []), list):
        raise ValueError("known_devices must be a JSON array of names")
    return settings, source


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("the settings file must contain a JSON object")
    for section in ("monitor", "thresholds", "speech"):
        if section in settings and not isinstance(settings[section], dict):
            raise ValueError("{} must be a JSON object".format(section))
    names = settings.get("known_devices", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("known_devices must be an array of device-name strings")
    if not isinstance(settings.get("assistant_name", "Cortana"), str):
        raise ValueError("assistant_name must be a string")

    monitor = settings.setdefault("monitor", {})
    interval = float(monitor.get("interval_seconds", 30))
    scan_seconds = float(monitor.get("bluetooth_scan_seconds", 6))
    scan_every = int(monitor.get("bluetooth_every_n_cycles", 5))
    if not math.isfinite(interval) or interval < 5:
        raise ValueError("monitor.interval_seconds must be at least 5")
    if not math.isfinite(scan_seconds) or not 1 <= scan_seconds <= 60:
        raise ValueError("monitor.bluetooth_scan_seconds must be between 1 and 60")
    if scan_every < 1:
        raise ValueError("monitor.bluetooth_every_n_cycles must be at least 1")
    voice = settings.get("speech", {}).get("voice", "")
    if not isinstance(voice, str):
        raise ValueError("speech.voice must be a string")


def status_line(snapshot):
    system = snapshot["system"]
    battery = system["battery"]
    thermal = system["thermal"].get("state") or "unknown"
    wifi = snapshot["wifi"]
    radio = wifi.get("radio") or {}
    rssi = radio.get("rssi_dbm")
    loss = wifi.get("gateway_probe", {}).get("packet_loss_pct")
    ble = snapshot["bluetooth"]

    fields = []
    if battery.get("percent") is not None:
        fields.append("battery {}%".format(battery["percent"]))
    else:
        fields.append("battery unavailable")
    fields.append("thermal {}".format(thermal))
    if isinstance(rssi, (int, float)) and rssi != 0:
        fields.append("Wi-Fi {} dBm".format(rssi))
    else:
        fields.append("Wi-Fi signal unavailable")
    if loss is not None:
        fields.append("gateway loss {}%".format(loss))
    if ble.get("scanned"):
        fields.append("BLE saw {} peripherals".format(len(ble.get("nearby", []))))
    elif ble.get("error"):
        fields.append("BLE unavailable")
    else:
        fields.append("BLE scan skipped")
    return "  |  ".join(fields)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Local-first MacBook diagnostics with a Cortana-inspired persona."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="take one snapshot (default)")
    mode.add_argument("--watch", action="store_true", help="monitor at the configured interval")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "settings.json",
        help="settings JSON file (defaults to settings.json)",
    )
    parser.add_argument("--scan-wifi", action="store_true", help="scan nearby Wi-Fi once")
    parser.add_argument("--no-bluetooth", action="store_true", help="skip all Bluetooth access")
    speech = parser.add_mutually_exclusive_group()
    speech.add_argument("--speak", dest="speak", action="store_true",
                        help="speak persona lines with the local macOS voice")
    speech.add_argument("--no-speak", dest="speak", action="store_false",
                        help="disable voice output for this run")
    parser.add_argument("--json", action="store_true", help="print full JSON snapshots")
    parser.set_defaults(speak=None)
    return parser.parse_args()


def main():
    args = parse_args()
    repo_root = Path(__file__).resolve().parent
    example_path = repo_root / "config.example.json"
    try:
        settings, settings_source = load_settings(args.config, example_path)
        validate_settings(settings)
    except ValueError as error:
        print("Configuration error: {}".format(error), file=sys.stderr)
        return 2

    if settings_source != args.config:
        print(
            "Using config.example.json. Copy it to settings.json to customize local settings.",
            file=sys.stderr,
        )

    speech_config = settings.get("speech", {})
    speak = speech_config.get("enabled", False) if args.speak is None else args.speak
    voice = speech_config.get("voice", "")
    assistant_name = settings.get("assistant_name", "Cortana")
    monitor = settings.get("monitor", {})
    interval = float(monitor.get("interval_seconds", 30))
    bluetooth_every = int(monitor.get("bluetooth_every_n_cycles", 5))

    bridge = MacHardwareBridge(repo_root, settings)
    persona = CortanaPersonaEngine(settings)
    iteration = 0
    last_persona_state = "IDLE"
    watch = args.watch

    try:
        while True:
            iteration += 1
            do_bluetooth = (
                not args.no_bluetooth
                and last_persona_state != "COOLING"
                and (iteration - 1) % bluetooth_every == 0
            )
            snapshot = bridge.snapshot(
                scan_wifi=args.scan_wifi and iteration == 1,
                scan_bluetooth=do_bluetooth,
            )
            event = persona.update(snapshot)
            last_persona_state = event["state"]
            snapshot["timestamp"] = datetime.now(timezone.utc).isoformat()
            snapshot["persona"] = event

            if args.json:
                print(json.dumps(snapshot, indent=2, sort_keys=True, default=str), flush=True)
            else:
                print(status_line(snapshot), flush=True)
                if event.get("message"):
                    print("{}: {}".format(assistant_name, event["message"]), flush=True)
                elif iteration == 1:
                    print("{}: Diagnostics are up. I’ll keep the chatter on a cooldown.".format(
                        assistant_name
                    ), flush=True)

            if event.get("message") and speak:
                speech_error = bridge.speak(event["message"], voice=voice)
                if speech_error:
                    print("Voice output unavailable: {}".format(speech_error), file=sys.stderr)

            if not watch:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
