# aiogrilla

[![PyPI version](https://img.shields.io/pypi/v/aiogrilla.svg)](https://pypi.org/project/aiogrilla/)
[![Python versions](https://img.shields.io/pypi/pyversions/aiogrilla.svg)](https://pypi.org/project/aiogrilla/)
[![CI](https://img.shields.io/github/actions/workflow/status/zwrose/aiogrilla/ci.yml?branch=main&label=CI&logo=github)](https://github.com/zwrose/aiogrilla/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/zwrose/aiogrilla/blob/main/LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

Unofficial async Python client for Grilla Grills Alpha Connect smokers.
It provides read-only access to live grill and probe temperatures, cook status,
cook mode, and the cook timer via the vendor's cloud service.

## Disclaimer

**aiogrilla is an unofficial, third-party library. It is not affiliated with,
endorsed by, or supported by Grilla Grills or Fahrenheit Technologies, Inc.
"Grilla" is used here as a nominative reference to identify the product this
library works with. This library is provided as-is and may stop working at any
time if the vendor changes their cloud service. Use is entirely at your own risk,
with no warranty of any kind.**

## Install

```bash
pip install aiogrilla
```

### From source

```bash
git clone https://github.com/zwrose/aiogrilla.git
cd aiogrilla
pip install -e .
```

## Usage

Run the bundled example — it prompts for your password and never stores it:

```bash
export GRILLA_EMAIL="your@email.com"   # optional; the example prompts if unset
python examples/dump_state.py
```

Or use the library directly. Log in **once** with your password to obtain a refresh
token, then persist and reuse that token — the password is never needed again and never
has to be stored:

```python
import asyncio
import getpass
from aiogrilla import GrillaClient


async def main(email: str, password: str) -> None:
    client = GrillaClient()
    refresh = await client.async_login_with_password(email, password)
    # Persist `refresh` securely (e.g. a secrets manager) and reconnect later with
    # GrillaClient(refresh_token=refresh) — no password storage required.
    grills = await client.async_get_grills()
    print("grills:", grills)
    if not grills:
        return
    grill = grills[0]
    client.on_state(grill.id, lambda s: print("state:", s.mode, s.grill_temp, s.probe_temp))
    client.on_availability(grill.id, lambda a: print("available:", a))
    await client.async_connect()
    await asyncio.sleep(30)
    await client.async_disconnect()


asyncio.run(main(input("Grilla email: "), getpass.getpass("Grilla password: ")))
```

## What it does / v1 scope

aiogrilla is intentionally read-only in its first release:

- Live grill temperature and target temperature
- Probe temperature(s) and target probe temperature(s)
- Cook status / operating mode (off, igniting, running, hold, shutdown, etc.)
- Cook timer (total and remaining seconds)
- Grill availability (connected / disconnected)

Write operations (set temperature, start/stop cook, etc.) are out of scope for
v1.

## Tested hardware

aiogrilla has so far been verified against a **single grill**:

| Model | Controller | Firmware |
| --- | --- | --- |
| Grilla Silverbac 2.0 XL Built-In | Alpha Connect 2.0 | 1.0.70 |

Other Alpha Connect grills share the same cloud API and **should** work, but are
unverified. **If you have a different model, please try it and [open an issue](https://github.com/zwrose/aiogrilla/issues/new?template=compatibility_report.yml)
with your findings** — model, firmware, and any fields that look off (a redacted
state sample helps — see Caveats). Other owners' reports are how compatibility gets
confirmed.

## Caveats

The field mapping is best-effort and validated against a limited set of devices
and firmware. Some fields (probe 2, turntable, alarm range) may not apply to all
grill models or firmware versions. If you observe unexpected parsing behavior,
please open an issue with a **redacted** (no credentials or tokens) sample.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). This project
uses [Conventional Commits](https://www.conventionalcommits.org/) and
release-please, so commit messages drive versioning and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
