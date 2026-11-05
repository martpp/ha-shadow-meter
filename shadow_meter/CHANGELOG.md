# Changelog

All notable changes to this app are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this app uses
[Semantic Versioning](https://semver.org/).

## [0.3.2] - 2026-11-05

### Added
- **Find the phases automatically** (Settings, devices with their own meter): looks at 14 days of the
  InfluxDB (or 7 days of the recorder) and assigns every smart plug to the phase that changes whenever
  its device switches. Devices behind a UPS or on another circuit are recognised and left out. In a test
  household 11 plugs were assigned, none wrongly; plugs whose devices did not switch stay unassigned
  (they do not matter for the detection).

### Changed
- Settings explains that devices with their own meter should be assigned: if they are not, their
  switching (heaters, kitchen appliances, printers, ...) is "detected" again as heating elements and
  fridges. Until now the hint said they only make the results "a bit noisier", which was wrong.

## [0.3.1] - 2026-11-03

### Fixed
- Candidates no longer pop up and vanish while a long history is analysed. In a test with ten months
  of data a fridge was below 50 % on 169 of 247 days (mostly at 0 %, for weeks at a time); now only on
  its first two days:
  - The type probabilities are smoothed over about two weeks of data (weighted by new evidence, so a
    young candidate still settles quickly). A noisy week no longer turns a fridge into "unknown".
  - The spread of the run times only uses plausible cycles (not < 2 min or > 3x the typical run time,
    which are mostly mis-paired steps), and varying run times lower the fridge score instead of ruling
    it out (a fridge runs longer in summer and after a defrost).
  - A compressor runs at least about 8 minutes; shorter regular "cycles" are noise, not a fridge.
  - Fragments of a device - a few hundred of its cycles that ended up in a group of their own, e.g. a
    "fridge" of 30 W next to the real one of 38 W - are linked to it ("Probably part of the same device
    as ...") and only shown with "show uncertain".

### Changed
- The confidence graph on a card covers the whole lifetime of the candidate, with a time axis (dates
  or times, depending on the span) and a line at 70 %. The history is thinned out with age instead of
  keeping only the newest points, so it reaches back to the start of the analysis.

## [0.3.0] - 2026-11-03

### Added
- **Reactive power (optional):** choose the power factor sensor of each phase in Settings (suggested for
  Shelly 3EM / Pro 3EM). Every switch-on step then also gets its reactive/real power ratio dQ/dP:
  heating elements are ~0, motors and power supplies clearly above. Devices with the same power but a
  different kind of load are kept apart and never merged, and a type that does not fit the measured
  ratio (a "heating element" that behaves like a motor) loses its confidence. The cards show the ratio.
  Without a power factor sensor nothing changes. Adding one does not restart the analysis.
- InfluxDB: the power factor sensors are read from the measurement `%` (option
  `power_factor_measurement`), together with the power.

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
