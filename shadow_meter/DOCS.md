# Shadow Meter

Shadow Meter detects devices that have no meter of their own from your whole-home power consumption.
It works best with a meter that measures each phase every few seconds (Shelly 3EM, Shelly Pro 3EM, ...),
but a single-phase meter works too.

## How it works

1. **Steps.** Every time a device switches, the power of its phase jumps. Shadow Meter finds these steps
   (ignoring single spikes) after subtracting the devices you already measure with smart plugs.
2. **Cycles.** A switch-on step and the matching switch-off step form a cycle: "~37 W for 20 minutes".
3. **Candidates.** Similar cycles on the same phase are grouped. A group that runs 20 times a day at any
   hour with 30-250 W looks like a fridge; one with 2 kW for a few minutes at meal times looks like a kettle
   or a hob. Two groups with a similar power that take turns (never run at the same time) are one device
   whose power drifts, and are merged. The confidence combines how well the pattern fits a type, how much evidence there is, and how
   consistent the power is.
4. **Context.** Home Assistant knows when your lights are switched. If the power steps by the same amount
   every time a light switches on its own, that is the power of the light.
5. **Reactive power (optional).** If your meter also reports the power factor of each phase (Shelly 3EM,
   Shelly Pro 3EM, ...), Shadow Meter measures how much the reactive power changes with every switch-on
   step (dQ/dP). Heating elements are purely resistive (~0), motors and power supplies are not (a fridge
   compressor is ~0.2). Devices with the same power but a different kind of load are kept apart, and a
   "heating element" that behaves like a motor is not called one. Without a power factor sensor everything
   works on the real power alone.

You see all candidates in the Shadow Meter panel, can rename them, change the type, confirm or reject
them, and **add them to Home Assistant**. An added device gets three entities: running (on/off), power (W)
and energy (kWh, for the Energy dashboard).

## Installation

1. Add this repository to the app store (Settings → Apps → App store → ⋮ → Repositories).
2. Install **Shadow Meter**.
3. Start the app and open the **Shadow Meter** panel in the sidebar.
4. Choose your meters in **Settings** (opens by itself on the first start): the power sensor of each
   phase, optionally its power factor sensor, and optionally the phase of devices that already have their
   own meter. Shadow Meter suggests the phases of common 3-phase meters (Shelly 3EM, Shelly Pro 3EM, meter
   readers with L1-L3) and the power factor sensors that belong to them. Adding or removing a power factor
   sensor does not restart the analysis; it applies to the cycles from then on (use **Analyse the history
   again** to apply it to the history as well).

The meters are chosen in the panel because the app configuration page of Home Assistant cannot show an
entity picker. Changing the meters later restarts the analysis from the beginning; devices you
confirmed from lights are kept.

To add detected devices to Home Assistant you need an MQTT broker (e.g. the Mosquitto app) and the
MQTT integration. If the broker runs as an app, Shadow Meter finds it automatically.

## Options

The meters are not listed here: they are chosen in the panel (Settings).

```yaml
context_domains:
  - light
context_entities: []
exclude_entities: []
history_days: 14
min_step_w: 12
influx:
  enabled: false
mqtt:
  host: ""
log_level: info
```

| Option | Description |
|---|---|
| `context_domains` | Entities of these domains are matched with power steps (default: `light`). Switches of smart plugs are usually better left out because their power is metered anyway. |
| `context_entities` | Further entities to match, e.g. a switch that powers an unmetered device. |
| `exclude_entities` | Entities never to match, e.g. status LEDs of devices. |
| `history_days` | Days of Home Assistant recorder history to analyse at the first start (the recorder keeps 10 days by default). |
| `min_step_w` | Power changes below this value are ignored. |
| `influx` | Optional: analyse years of history from an InfluxDB 1.x written by the Home Assistant InfluxDB integration. `url`, `database`, `username`/`password` (a read-only user is enough), `power_measurement` (default `W`), `legacy_state_measurement` (older Home Assistant versions wrote all states into `state`), `power_factor_measurement` (default `%`, for the optional power factor sensors), `history_start` (`YYYY-MM-DD`, optional). |
| `mqtt` | Leave `host` empty to use the MQTT service of the Supervisor. Otherwise `host`, `port`, `username`, `password`. `discovery_prefix` must match the MQTT integration (default `homeassistant`). |
| `log_level` | `debug`, `info`, `warning` or `error`. |

The analysis state is kept in the app's data folder. A restart continues where it stopped.

**Linking an InfluxDB later.** The analysis only moves forward in time, so a restart after linking an
InfluxDB uses it for new data only. To include the older history, enter the `influx` options, restart
the app, open **Settings** in the panel and choose **Analyse the history again**. This starts the
analysis over: the detected devices are discovered anew (their names, types and confirmations are lost,
and pattern devices you added to Home Assistant are removed there); devices confirmed from lights are kept.

How long the first analysis takes depends mostly on how fast the history can be read: in a test with
3.5 months of InfluxDB data (3 phases, ~30 sub-meters, ~25 lights) it took about 9 minutes, most of it
waiting for the database. Years of history can take an hour or more; the panel shows the progress.

## Running it on another machine

The analysis of long histories needs some CPU and around 300 MB of RAM. If that is too much for your
Raspberry Pi, run the engine as a normal container on another machine and connect it to Home Assistant
with a long-lived access token - see the project README for `docker compose`.

The meters are chosen in its web UI as well. For a setup without the UI, `options.json` also accepts
`phases`, with one entry per phase: `phase` (name), `mains` (power sensor of the phase, W) and `submeters`
(power sensors of devices on that phase with their own meter). A setup saved in the UI takes precedence.

## Security and privacy

- Everything runs locally. Power data reveals a lot about a household (presence, daily routine); nothing
  leaves your network.
- The web UI is only reachable through Home Assistant (ingress); the app refuses all other connections.
- The app reads from Home Assistant through the WebSocket API (`homeassistant_api: true`). This proxy
  grants administrator rights, so the app only uses read-only commands: configuration, entity list,
  history and state subscriptions. It never calls services or changes anything in Home Assistant.
- InfluxDB access is read-only; only `SELECT`/`SHOW` queries are sent and all names are validated.

## Known limitations

- Devices that always switch together (scenes) can only be estimated together.
- Small devices (below ~10 W), devices with a continuously varying power (heat pumps with inverters,
  computers) and several identical devices are hard or impossible to separate.
- Homes with solar panels: the meter must show consumption, not the net grid power. Not tested yet.
- The device types are a simple, transparent rule set, not a trained model. The confidence is a guide,
  not a guarantee - confirm what you know.

## Support

Please report problems and ideas in the GitHub issues of the project. Include the log of the app and,
if possible, what Shadow Meter found and what is really in your home.

## License

MIT
