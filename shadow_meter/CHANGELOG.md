# Changelog

All notable changes to this app are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this app uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-30

### Added
- Choose the meters in the panel (Settings): lists the power sensors of Home Assistant with their
  current value, suggests the phases of common 3-phase meters, assigns devices with their own meter to
  a phase. Opens by itself when nothing is set up yet.

### Changed
- The `phases` option is now optional (and empty by default). A setup saved in the panel takes
  precedence; an existing `phases` option keeps working.
- Changing the meters restarts the analysis from the beginning; pattern devices that were added to
  Home Assistant are removed, devices confirmed from lights are kept.

## [0.1.0] - 2026-10-30

### Added
- First experimental version: step detection on the whole-home power per phase, on/off cycles,
  device candidates (fridge/freezer, heating elements, unknown) with confidence, matching of light
  switching with power steps.
- History analysis from the Home Assistant recorder or an InfluxDB 1.x, then live analysis.
- Web UI in the Home Assistant sidebar: progress, activity, candidates with confidence trend; confirm,
  reject, rename, change type.
- Add confirmed devices to Home Assistant via MQTT discovery (running, power, energy).
