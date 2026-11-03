# Changelog

All notable changes to this app are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this app uses
[Semantic Versioning](https://semver.org/).

## [0.2.3] - 2026-11-03

### Added
- **Analyse the history again** in Settings: starts the analysis over with all history sources, e.g.
  after linking an InfluxDB (until now the analysis only continued where it stopped, so older history
  of a newly linked InfluxDB was never read). Asks for confirmation in the panel, because the detected
  devices are discovered anew; devices confirmed from lights are kept.
- Settings shows where the history comes from (Home Assistant recorder or InfluxDB).

## [0.2.2] - 2026-11-03

### Fixed
- "Running … of the time" and the kWh/day estimate were far too high when a single switch-off step was
  missed (one cycle then lasted for hours). The run time per cycle is now capped at three times the
  typical run time.
- One device could show up as two candidates when its power drifts (e.g. a fridge compressor drawing
  34 W on some runs and 39 W on others). Candidates on the same phase that are close in power and take
  turns - they run in the same period but practically never at the same time - are now merged. This
  also applies to an analysis that is already running. Candidates you have confirmed, renamed, rejected
  or added to Home Assistant are never merged away.

## [0.2.1] - 2026-11-02

### Removed
- The `phases` field of the app configuration page: it could only offer free-text fields. The meters are
  chosen in the panel (Settings), which lists the power sensors of Home Assistant. If you entered phases
  there, Settings opens after the update; choose your meters again. The standalone container still
  accepts `phases` in `options.json`.

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
