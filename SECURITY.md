# Security policy

## Supported versions

Only the latest released version of Shadow Meter receives security fixes.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private vulnerability
reporting instead: *Security* tab → *Report a vulnerability*. You will get an answer within a week.

Please include what is affected (app or standalone mode, version), how to reproduce it, and the impact.

## Security model

- **App mode:** the web UI and API are only reachable through Home Assistant ingress. Home Assistant
  authenticates the user; the app refuses every connection that does not come from the Supervisor's
  ingress proxy.
- **Standalone mode:** the API requires a bearer token (`SHADOW_METER_API_TOKEN`) unless it listens on
  localhost only. Do not expose it to the internet.
- **Home Assistant access:** only read-only WebSocket commands are used (configuration, entity list,
  history, state subscriptions). The app never calls services.
- **InfluxDB access:** read-only; only `SELECT`/`SHOW` queries; all identifiers are validated because
  InfluxQL has no bind parameters.
- **Input:** all options and API requests are validated (pydantic); the UI inserts data only as text.
- **Secrets** (tokens, passwords) are never logged; HTTP access logs are disabled so tokens in URLs
  cannot leak.
- **Supply chain:** dependencies are pinned and updated by Dependabot; CI runs linting, strict type
  checking and tests on every change.
