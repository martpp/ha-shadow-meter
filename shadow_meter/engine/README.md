# Shadow Meter engine

The Python package behind the Shadow Meter app for Home Assistant: data sources, detection pipeline,
storage, web API and UI. It runs inside the app container, or standalone on any machine
(`python -m shadow_meter`, or the `Dockerfile` in this folder).

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/pytest            # tests (synthetic household, fake Home Assistant / InfluxDB)
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy              # strict type checking
```

## Layout

```
src/shadow_meter/
├── __main__.py      entry point: wires everything together, readable option errors
├── settings.py      validated options (app options.json / standalone) and runtime
├── models.py        small value types: Sample, Step, StateChange, Cycle
├── analyzer.py      backfill -> catch-up -> live; events for the UI; energy of active devices
├── store.py         SQLite on a single worker thread
├── detection/       the device detection
│   ├── grid.py        irregular sensor reports -> regular 5 s grid (carried forward until the next report)
│   ├── steps.py       streaming step detection (median filter, local maxima)
│   ├── cycles.py      pair switch-on with switch-off steps
│   ├── clusters.py    online clustering of cycles into candidates, features
│   ├── classify.py    rule-based device type + confidence
│   ├── context.py     match Home Assistant entity changes (lights, ...) with steps, corrected for chance
│   └── pipeline.py    the whole pipeline with serialisable state
├── sources/         where the data comes from
│   ├── homeassistant.py  WebSocket client (history, live)
│   └── influx.py         read-only InfluxDB 1.x client
├── mqtt/            how devices get into Home Assistant
│   ├── link.py        self-healing MQTT connection
│   └── discovery.py   MQTT discovery messages and states
└── web/             API, access control, UI
    ├── server.py
    └── static/        index.html, app.js, style.css, icon.svg
```
