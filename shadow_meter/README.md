# Shadow Meter

A shadow meter for every device that has no meter.

Shadow Meter analyses your whole-home power consumption (e.g. a Shelly 3EM) together with the on/off
history of your lights, and detects devices that have no meter of their own: the fridge, the kettle and
stove, lights, ... Each detection comes with a confidence that grows as more evidence arrives. Confirm a
device with one click and it appears in Home Assistant with power and energy - usable in the Energy
dashboard.

- Analyses the history first (Home Assistant recorder, or years of InfluxDB data), then follows live data
- Runs locally, no cloud
- Experimental: expect rough edges and please report what it finds (and misses) in your home
