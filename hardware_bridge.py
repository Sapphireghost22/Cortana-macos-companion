#!/usr/bin/env python3
"""Local-only adapters for Wi-Fi, BLE, battery, thermal, CPU, and memory data.

Made with [Chat Gpt Luna]
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    BleakClient = None
    BleakScanner = None


BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


def _run(command, timeout=15):
    try:
        return subprocess.run(
            command, text=True, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, error


def _walk_json(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _profile_snapshot(data_type):
    result = _run(["system_profiler", "-json", data_type], timeout=25)
    if isinstance(result, tuple):
        return None, str(result[1])
    if result.returncode != 0:
        return None, result.stderr.strip() or "system_profiler failed"
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "system_profiler returned invalid JSON"


class MacHardwareBridge:
    def __init__(self, repo_root, settings):
        self.repo_root = Path(repo_root)
        self.settings = settings
        self.cache_dir = self.repo_root / ".cache"
        self.native_binary = self.cache_dir / "mac_bridge"
        self._profile_cache = {}

    def _ensure_native_bridge(self):
        source = self.repo_root / "native" / "mac_bridge.swift"
        if not source.is_file():
            raise RuntimeError("native/mac_bridge.swift is missing")
        if not shutil.which("swiftc"):
            raise RuntimeError("swiftc is unavailable; install Xcode Command Line Tools")
        needs_build = not self.native_binary.is_file()
        if not needs_build:
            needs_build = source.stat().st_mtime > self.native_binary.stat().st_mtime
        if needs_build:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            result = _run(
                [
                    "swiftc",
                    str(source),
                    "-framework",
                    "CoreWLAN",
                    "-framework",
                    "Foundation",
                    "-o",
                    str(self.native_binary),
                ],
                timeout=90,
            )
            if isinstance(result, tuple):
                raise RuntimeError(str(result[1]))
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Swift bridge compilation failed")

    def _native(self, *arguments):
        try:
            self._ensure_native_bridge()
            result = _run([str(self.native_binary), *arguments], timeout=35)
            if isinstance(result, tuple):
                return None, str(result[1])
            if result.returncode != 0 and not result.stdout.strip():
                return None, result.stderr.strip() or "Native bridge returned an error"
            try:
                return json.loads(result.stdout), None
            except json.JSONDecodeError:
                return None, result.stderr.strip() or "Native bridge returned invalid JSON"
        except (OSError, RuntimeError) as error:
            return None, str(error)

    @staticmethod
    def _wifi_device():
        result = _run(["networksetup", "-listallhardwareports"])
        if isinstance(result, tuple) or result.returncode != 0:
            return None
        blocks = re.split(r"\n\s*\n", result.stdout)
        for block in blocks:
            if re.search(r"Hardware Port:\s*Wi-Fi", block, re.IGNORECASE):
                match = re.search(r"Device:\s*(\S+)", block)
                if match:
                    return match.group(1)
        return None

    @staticmethod
    def _gateway_probe():
        route = _run(["route", "-n", "get", "default"])
        if isinstance(route, tuple) or route.returncode != 0:
            return {"gateway": None, "packet_loss_pct": None, "avg_ms": None}
        match = re.search(r"^\s*gateway:\s*(\S+)", route.stdout, re.MULTILINE)
        if not match:
            return {"gateway": None, "packet_loss_pct": None, "avg_ms": None}
        gateway = match.group(1)
        result = _run(["ping", "-c", "3", "-W", "1000", gateway], timeout=7)
        if isinstance(result, tuple):
            return {
                "gateway": gateway,
                "packet_loss_pct": None,
                "avg_ms": None,
                "error": str(result[1]),
            }
        loss = re.search(r"([\d.]+)% packet loss", result.stdout)
        average = re.search(
            r"round-trip min/avg/max/(?:stddev|mdev)\s*=\s*[\d.]+/([\d.]+)/",
            result.stdout,
        )
        return {
            "gateway": gateway,
            "packet_loss_pct": float(loss.group(1)) if loss else None,
            "avg_ms": float(average.group(1)) if average else None,
        }

    def wifi_snapshot(self, scan=False):
        args = ["wifi"]
        if scan:
            args.append("--scan")
        radio, error = self._native(*args)
        device = self._wifi_device()
        active_network = None
        if device:
            result = _run(["networksetup", "-getairportnetwork", device])
            if not isinstance(result, tuple) and result.returncode == 0:
                active_network = result.stdout.strip()
        return {
            "device": device,
            "active_network": active_network,
            "radio": radio,
            "error": error,
            "gateway_probe": self._gateway_probe(),
        }

    def _cached_profile(self, data_type, max_age=300):
        now = time.monotonic()
        cached = self._profile_cache.get(data_type)
        if cached and now - cached[0] < max_age:
            return cached[1], cached[2]
        payload, error = _profile_snapshot(data_type)
        self._profile_cache[data_type] = (now, payload, error)
        return payload, error

    def _battery_snapshot(self):
        result = _run(["pmset", "-g", "batt"])
        power_source = None
        percent = None
        if not isinstance(result, tuple) and result.returncode == 0:
            source_match = re.search(r"Now drawing from '([^']+)'", result.stdout)
            percent_match = re.search(r"\b(\d+)%", result.stdout)
            power_source = source_match.group(1) if source_match else None
            percent = int(percent_match.group(1)) if percent_match else None

        payload, error = self._cached_profile("SPPowerDataType")
        cycle_count = None
        condition = None
        if payload:
            for key, value in _walk_json(payload):
                normalized = re.sub(r"[^a-z]", "", key.casefold())
                if cycle_count is None and "cycle" in normalized and "count" in normalized:
                    found = re.search(r"\d+", str(value))
                    if found:
                        cycle_count = int(found.group(0))
                if condition is None and (
                    normalized.endswith("condition")
                    or normalized.endswith("batteryhealth")
                ):
                    condition = str(value)
        return {
            "percent": percent,
            "power_source": power_source,
            "cycle_count": cycle_count,
            "condition": condition,
            "profile_error": error,
        }

    @staticmethod
    def _cpu_snapshot():
        result = _run(["ps", "-A", "-o", "%cpu="])
        if isinstance(result, tuple) or result.returncode != 0:
            return {"approx_pct": None, "logical_cpus": os.cpu_count()}
        values = []
        for value in result.stdout.split():
            try:
                values.append(float(value))
            except ValueError:
                continue
        logical_cpus = os.cpu_count() or 1
        return {
            "approx_pct": round(min(100.0, sum(values) / logical_cpus), 1),
            "logical_cpus": logical_cpus,
            "method": "approximate process CPU sum normalized by logical CPU count",
        }

    @staticmethod
    def _ram_snapshot():
        total_result = _run(["sysctl", "-n", "hw.memsize"])
        stats = _run(["vm_stat"])
        if (
            isinstance(total_result, tuple)
            or total_result.returncode != 0
            or isinstance(stats, tuple)
            or stats.returncode != 0
        ):
            return {"used_pct_approx": None, "used_bytes_approx": None, "total_bytes": None}
        try:
            total_bytes = int(total_result.stdout.strip())
        except ValueError:
            return {"used_pct_approx": None, "used_bytes_approx": None, "total_bytes": None}
        page_match = re.search(r"page size of (\d+) bytes", stats.stdout)
        page_size = int(page_match.group(1)) if page_match else 4096
        pages = {}
        for line in stats.stdout.splitlines():
            match = re.match(r"Pages\s+(.+?):\s+([\d.]+)", line)
            if match:
                key = re.sub(r"[^a-z]", "", match.group(1).casefold())
                pages[key] = int(match.group(2).rstrip("."))
        free_pages = pages.get("free", 0) + pages.get("speculative", 0)
        used_bytes = max(0, total_bytes - free_pages * page_size)
        return {
            "used_pct_approx": round(100.0 * used_bytes / total_bytes, 1) if total_bytes else None,
            "used_bytes_approx": used_bytes,
            "total_bytes": total_bytes,
            "method": "physical memory minus free and speculative pages",
        }

    def _thermal_snapshot(self):
        payload, error = self._native("thermal")
        return {"state": payload.get("thermal_state") if payload else None, "error": error}

    @staticmethod
    def _matches_known(name, known_devices):
        folded = name.casefold()
        return any(token and token.casefold() in folded for token in known_devices)

    @staticmethod
    def _collect_battery_fields(node, name=None, rows=None):
        if rows is None:
            rows = []
        if isinstance(node, dict):
            local_name = name
            for key, value in node.items():
                normalized = re.sub(r"[^a-z]", "", str(key).casefold())
                if normalized in ("name", "devicename", "devicelocalname") and isinstance(value, str):
                    local_name = value
            fields = {
                str(key): value
                for key, value in node.items()
                if "battery" in re.sub(r"[^a-z]", "", str(key).casefold())
                and isinstance(value, (str, int, float))
            }
            if fields:
                rows.append({"device": local_name or "macOS-reported device", "fields": fields})
            for key, value in node.items():
                child_name = local_name
                normalized = re.sub(r"[^a-z]", "", str(key).casefold())
                if (
                    child_name is None
                    and isinstance(value, (dict, list))
                    and not normalized.startswith(("sp", "device", "controller"))
                ):
                    child_name = str(key)
                MacHardwareBridge._collect_battery_fields(value, child_name, rows)
        elif isinstance(node, list):
            for value in node:
                MacHardwareBridge._collect_battery_fields(value, name, rows)
        return rows

    async def _scan_ble(self, seconds, known_devices, read_gatt_battery):
        discovered = {}

        def on_discovery(device, advertisement):
            item = discovered.setdefault(
                device.address,
                {
                    "device": device,
                    "name": advertisement.local_name or device.name or "Unnamed BLE peripheral",
                    "rssi_dbm": advertisement.rssi,
                    "service_uuids": advertisement.service_uuids or [],
                },
            )
            item["name"] = advertisement.local_name or device.name or item["name"]
            item["rssi_dbm"] = advertisement.rssi
            item["service_uuids"] = advertisement.service_uuids or []

        scanner = BleakScanner(detection_callback=on_discovery)
        await scanner.start()
        try:
            await asyncio.sleep(seconds)
        finally:
            await scanner.stop()

        rows = []
        for item in discovered.values():
            name = item["name"]
            row = {
                "name": name,
                "rssi_dbm": item["rssi_dbm"],
                "service_uuids": item["service_uuids"],
                "known_match": self._matches_known(name, known_devices),
            }
            if read_gatt_battery and row["known_match"]:
                try:
                    async with BleakClient(item["device"], timeout=8) as client:
                        value = await client.read_gatt_char(BATTERY_LEVEL_UUID)
                    row["gatt_battery_pct"] = int(value[0])
                except Exception as error:
                    row["gatt_battery_error"] = type(error).__name__
            rows.append(row)
        rows.sort(key=lambda row: row.get("rssi_dbm", -999), reverse=True)
        return rows

    def bluetooth_snapshot(self, scan=True):
        if not scan:
            return {"scanned": False, "nearby": [], "battery_reports": [], "error": None}
        if sys.platform != "darwin":
            return {
                "scanned": False,
                "nearby": [],
                "battery_reports": [],
                "error": "Bluetooth scanning is supported only on macOS",
            }
        if BleakScanner is None:
            return {
                "scanned": False,
                "nearby": [],
                "battery_reports": [],
                "error": "Bleak is not installed; install requirements.txt",
            }

        monitor = self.settings.get("monitor", {})
        known_devices = self.settings.get("known_devices", [])
        scan_seconds = float(monitor.get("bluetooth_scan_seconds", 6))
        read_battery = bool(monitor.get("read_gatt_battery", False))
        try:
            nearby = asyncio.run(
                self._scan_ble(scan_seconds, known_devices, read_battery)
            )
        except Exception as error:
            return {
                "scanned": False,
                "nearby": [],
                "battery_reports": [],
                "error": type(error).__name__,
            }

        payload, profile_error = self._cached_profile("SPBluetoothDataType", max_age=90)
        reports = self._collect_battery_fields(payload) if payload else []
        return {
            "scanned": True,
            "nearby": nearby,
            "battery_reports": reports,
            "error": profile_error,
        }

    def snapshot(self, scan_wifi=False, scan_bluetooth=True):
        battery = self._battery_snapshot()
        return {
            "system": {
                "battery": battery,
                "thermal": self._thermal_snapshot(),
                "cpu": self._cpu_snapshot(),
                "ram": self._ram_snapshot(),
            },
            "wifi": self.wifi_snapshot(scan=scan_wifi),
            "bluetooth": self.bluetooth_snapshot(scan=scan_bluetooth),
        }

    @staticmethod
    def speak(text, voice=""):
        command = ["say"]
        if voice:
            command.extend(["-v", voice])
        command.append(text)
        result = _run(command, timeout=60)
        if isinstance(result, tuple):
            return str(result[1])
        if result.returncode != 0:
            return result.stderr.strip() or "macOS say failed"
        return None
