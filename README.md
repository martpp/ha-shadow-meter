# Shadow Meter for Home Assistant

[![CI](https://github.com/martpp/ha-shadow-meter/actions/workflows/ci.yml/badge.svg)](https://github.com/martpp/ha-shadow-meter/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Status: experimental](https://img.shields.io/badge/status-experimental-orange.svg)

**A shadow meter for every device that has no meter.**

Shadow Meter analyses your whole-home power consumption (e.g. a Shelly 3EM) and the on/off history of
your lights, and detects devices that have no meter of their own - the fridge, the kettle and the stove,
your lights. Every detection comes with a confidence that rises (or falls) as more evidence arrives.
Confirm a device and it appears in Home Assistant with power and energy, ready for the Energy dashboard.

> **Status: experimental (0.1).** The detection works well for cycling appliances (fridges, freezers),
> large heating elements and lights that switch on their own. Help by testing it in your home and
> reporting what it finds and misses.

## Features

- **History first, then live:** analyses the Home Assistant recorder history - or years of data from an
  InfluxDB 1.x - day by day, then follows the live data. A restart continues where it stopped.
- **Explainable candidates:** "~37 W, 22 cycles/day, runs 15 min, any time of day → fridge, 84 %".
- **Context from Home Assistant:** light switching is matched with power steps, so lights get their real
  power - and their names.
- **One click to Home Assistant:** confirmed devices get *running*, *power* and *energy* entities via MQTT.
- **Local and private:** no cloud. The UI is only reachable through Home Assistant (ingress).
- **Runs on a Raspberry Pi**, or as a standalone container on another machine.

## Installation (Home Assistant app)

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmartpp%2Fha-shadow-meter)

1. Add this repository: Settings → Apps → App store → ⋮ → Repositories →
   `https://github.com/martpp/ha-shadow-meter`
2. Install **Shadow Meter**, set your power sensors in the options, start it.
3. Open **Shadow Meter** in the sidebar and watch it analyse your history.

See [the app documentation](shadow_meter/DOCS.md) for all options.

## Running the engine on another machine

If your Home Assistant host is too small for analysing years of history, run the engine as a container
elsewhere and connect it with a long-lived access token: see [`docker-compose.example.yml`](docker-compose.example.yml).

## How it works

Every switching device makes the power of its phase jump. Shadow Meter subtracts everything you already
measure, finds these steps, pairs switch-on and switch-off steps into cycles, groups similar cycles into
device candidates and types them by power, run time, how often and when they run. Home Assistant's own
knowledge (which light was switched when) turns many candidates into named devices. Details:
[`shadow_meter/DOCS.md`](shadow_meter/DOCS.md) and the [engine README](shadow_meter/engine/README.md).

The project started as an offline analysis of 3.5 years of data from one apartment; those scripts and
the results (fridge ≈ 115 kWh/year, stove ≈ 215 kWh/year, hallway light 21 W, ...) are in
[`research/`](research/README.md).

## Repository layout

| Path | Content |
|---|---|
| `shadow_meter/` | the Home Assistant app (config, Dockerfile, docs) |
| `shadow_meter/engine/` | the Python engine (package, tests, standalone Dockerfile) |
| `research/` | offline analysis scripts the project grew out of |

## Contributing, security

Contributions are welcome - see [CONTRIBUTING.md](CONTRIBUTING.md). Please report security problems
privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
