// Made with [Chat Gpt Luna]
// Minimal public-API bridge for CoreWLAN and Foundation thermal state.

import CoreWLAN
import Foundation

func emit(_ value: [String: Any]) {
    guard
        let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]),
        let text = String(data: data, encoding: .utf8)
    else {
        fputs("{\"error\":\"Could not encode bridge response\"}\n", stderr)
        exit(2)
    }
    print(text)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else {
    fputs("Usage: mac_bridge wifi [--scan] | thermal\n", stderr)
    exit(64)
}

switch command {
case "thermal":
    let state: String
    switch ProcessInfo.processInfo.thermalState {
    case .nominal:
        state = "nominal"
    case .fair:
        state = "fair"
    case .serious:
        state = "serious"
    case .critical:
        state = "critical"
    @unknown default:
        state = "unknown"
    }
    emit(["thermal_state": state])

case "wifi":
    guard let wifi = CWWiFiClient.shared().interface() else {
        emit(["error": "No Wi-Fi interface found", "nearby_networks": []])
        exit(1)
    }

    let shouldScan = arguments.contains("--scan")
    var nearby: [[String: Any]] = []
    var scanError = ""

    if shouldScan {
        do {
            let networks = try wifi.scanForNetworks(withName: nil, includeHidden: false)
            nearby = networks.map { network in
                [
                    "ssid": network.ssid ?? "",
                    "rssi_dbm": network.rssiValue,
                    "noise_dbm": network.noiseMeasurement,
                    "channel": network.wlanChannel?.channelNumber ?? -1
                ]
            }.sorted {
                ($0["rssi_dbm"] as? Int ?? -999) > ($1["rssi_dbm"] as? Int ?? -999)
            }
        } catch {
            scanError = error.localizedDescription
        }
    }

    emit([
        "ssid": wifi.ssid() ?? "",
        "rssi_dbm": wifi.rssiValue(),
        "noise_dbm": wifi.noiseMeasurement(),
        "nearby_networks": nearby,
        "scan_error": scanError
    ])

default:
    fputs("Unknown command: \(command)\n", stderr)
    exit(64)
}
