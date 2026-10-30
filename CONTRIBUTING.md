# Contributing

Thanks for helping! The most valuable contribution right now is **testing in your home**: tell us what
Shadow Meter detected, what it missed, and what your setup looks like (meter, sampling rate, phases,
solar panels yes/no). Please open an issue for that.

## Development setup

```bash
cd shadow_meter/engine
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -e .
.venv/bin/pytest
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy
```

All three must pass before a pull request is merged (CI checks them).

## Guidelines

- Keep the detection explainable: every number shown to the user should be traceable to data.
- Add a test for every bug fix and feature; the synthetic household in `tests/synthetic.py` makes it
  easy to add devices.
- No new runtime dependency without a good reason - the app has to run on a Raspberry Pi.
- Security first: validate input, never log secrets, keep Home Assistant access read-only.
- Do not commit real household data (it reveals a lot about a home); use synthetic data in tests.
- If you use AI tools, review and understand every change you submit and be able to explain it.

Optionally install the git hooks: `pip install pre-commit && pre-commit install`.

## Releases

1. Update the version in `shadow_meter/config.yaml`, `shadow_meter/engine/pyproject.toml` and
   `shadow_meter/engine/src/shadow_meter/__init__.py` (a test checks they match), and add the changes
   to `shadow_meter/CHANGELOG.md`.
2. Merge to `main`. The *Publish app image* workflow builds, signs and publishes the images for
   `aarch64` and `amd64` to GHCR. An existing version is never overwritten.
3. Tag the release (`git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`) and create a GitHub
   release with the changelog entry.

The first time an image is published, make the package public (GitHub → Packages → shadow-meter →
Package settings → Change visibility) and enable `image:` in `shadow_meter/config.yaml`, so users
download the prebuilt image instead of building it on their device.
