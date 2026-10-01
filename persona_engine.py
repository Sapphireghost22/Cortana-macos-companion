#!/usr/bin/env python3
"""Fact-grounded Cortana-inspired diagnostics persona.

Made with [Chat Gpt Luna]
"""

import time


class CortanaPersonaEngine:
    """Turn sensor snapshots into concise messages with hysteresis and cooldowns."""

    def __init__(self, settings):
        self.settings = settings
        self.thresholds = settings.get("thresholds", {})
        self.known_devices = settings.get("known_devices", [])
        self.cooldown = int(self.thresholds.get("speech_cooldown_seconds", 900))
        self.last_spoken = {}
        self.active = set()

        self._battery_low = False
        self._thermal_hot = False
        self._thermal_clear_count = 0
        self._network_bad_count = 0
        self._network_good_count = 0
        self._network_unstable = False
        self._device_state = {
            name: {"seen": 0, "missed": 0, "active": False}
            for name in self.known_devices
        }

    def _threshold(self, key, default):
        try:
            return float(self.thresholds.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _advance_latches(self, snapshot):
        system = snapshot.get("system", {})
        battery = system.get("battery", {})
        power_source = (battery.get("power_source") or "").casefold()
        percent = battery.get("percent")

        if "battery" in power_source and isinstance(percent, (int, float)):
            if percent <= self._threshold("low_battery_pct", 20):
                self._battery_low = True
            elif percent > self._threshold("battery_recovered_pct", 25):
                self._battery_low = False
        elif power_source and "battery" not in power_source:
            self._battery_low = False

        thermal = (system.get("thermal", {}).get("state") or "unknown").casefold()
        if thermal in ("serious", "critical"):
            self._thermal_hot = True
            self._thermal_clear_count = 0
        elif thermal in ("nominal", "fair") and self._thermal_hot:
            self._thermal_clear_count += 1
            if self._thermal_clear_count >= 2:
                self._thermal_hot = False
                self._thermal_clear_count = 0

        loss = snapshot.get("wifi", {}).get("gateway_probe", {}).get("packet_loss_pct")
        if isinstance(loss, (int, float)):
            if loss >= self._threshold("gateway_loss_warn_pct", 20):
                self._network_bad_count += 1
                self._network_good_count = 0
            elif loss < self._threshold("gateway_loss_clear_pct", 5):
                self._network_good_count += 1
                self._network_bad_count = 0
            else:
                self._network_bad_count = 0
                self._network_good_count = 0
        else:
            self._network_bad_count = 0
            self._network_good_count = 0

        if self._network_bad_count >= int(self._threshold("network_bad_samples", 2)):
            self._network_unstable = True
        if self._network_good_count >= int(self._threshold("network_good_samples", 2)):
            self._network_unstable = False

        bluetooth = snapshot.get("bluetooth", {})
        if bluetooth.get("scanned"):
            seen_names = [
                row.get("name", "").casefold()
                for row in bluetooth.get("nearby", [])
                if row.get("known_match")
            ]
            for configured_name, state in self._device_state.items():
                is_seen = any(configured_name.casefold() in name for name in seen_names)
                if is_seen:
                    state["seen"] += 1
                    state["missed"] = 0
                    if state["seen"] >= int(self._threshold("device_seen_scans", 2)):
                        state["active"] = True
                elif state["active"]:
                    state["missed"] += 1
                    state["seen"] = 0
                    if state["missed"] >= int(self._threshold("device_missing_scans", 3)):
                        state["active"] = False
                        state["missed"] = 0
                else:
                    state["seen"] = 0

        current = set()
        if self._battery_low:
            current.add("battery")
        if self._thermal_hot:
            current.add("thermal")
        if self._network_unstable:
            current.add("network")
        for name, state in self._device_state.items():
            if state["active"]:
                current.add("device:" + name)
        return current, percent, thermal, loss

    def _candidate_messages(self, active, percent, thermal, loss):
        candidates = []
        if "thermal" in active:
            if thermal == "critical":
                candidates.append((
                    "thermal_critical",
                    "Thermals are critical. I’m pausing optional work so the Mac can cool down.",
                    "COOLING",
                ))
            else:
                candidates.append((
                    "thermal_serious",
                    "Thermals are elevated. I’m pausing optional work so the Mac can shed some heat.",
                    "COOLING",
                ))
        if "battery" in active:
            if isinstance(percent, (int, float)):
                line = "We’re at {}%, and the charger is conspicuously absent. I can dim the theatrics and save some power.".format(int(percent))
            else:
                line = "Battery is low and we’re on battery power. I can dim the theatrics and save some power."
            candidates.append(("battery_low", line, "ALERT"))
        if "network" in active:
            line = "The gateway is missing checks. The local network path is having a day."
            if isinstance(loss, (int, float)):
                line = "The gateway is losing {:.0f}% of checks. The local network path is having a day.".format(loss)
            candidates.append(("network_loss", line, "ALERT"))
        for name in self.known_devices:
            if "device:" + name in active:
                candidates.append((
                    "device:" + name,
                    "I’m seeing a BLE signal from {}. Nearby signal, not a location fix.".format(name),
                    "RESPONDING",
                ))
        return candidates

    @staticmethod
    def _recovery_message(key):
        return {
            "thermal": "Thermal state is back in the comfortable range. Resuming low-priority diagnostics.",
            "battery": "Power is back in a comfortable range. I’ll keep the battery watch quiet.",
            "network": "The gateway is answering reliably again. Crisis downgraded to mild inconvenience.",
        }.get(key, "The signal from {} is no longer appearing.".format(key.partition(":")[2]))

    def update(self, snapshot, now=None):
        """Return a state and at most one fact-grounded line for a snapshot."""
        now = time.monotonic() if now is None else now
        previous = set(self.active)
        current, percent, thermal, loss = self._advance_latches(snapshot)
        self.active = current

        candidates = self._candidate_messages(current, percent, thermal, loss)
        if "thermal" in current:
            state = "COOLING"
        elif "battery" in current or "network" in current:
            state = "ALERT"
        elif current:
            state = "RESPONDING"
        else:
            state = "IDLE"

        for key, line, _candidate_state in candidates:
            if now - self.last_spoken.get(key, float("-inf")) >= self.cooldown:
                self.last_spoken[key] = now
                return {"state": state, "key": key, "message": line}

        recovered = previous - current
        if recovered:
            key = sorted(recovered)[0]
            recovery_key = "recovery:" + key
            if now - self.last_spoken.get(recovery_key, float("-inf")) >= self.cooldown:
                self.last_spoken[recovery_key] = now
                return {
                    "state": state,
                    "key": recovery_key,
                    "message": self._recovery_message(key),
                }
        return {"state": state, "key": None, "message": None}
