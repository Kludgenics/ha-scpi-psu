# scpi-psu

An asynchronous Python client for programmable SCPI bench power supplies. It supports serial and network serial connections using `serialx`, including standard SCPI and OWON-compatible dialects.

```python
from scpi_psu_client import ScpiPsuClient

client = ScpiPsuClient("socket://192.168.1.10:4001")
try:
    identity = await client.probe_identity()
    telemetry = await client.poll_telemetry()
finally:
    await client.close()
```

Install the package with `pip install scpi-psu`. For development, run `pip install -e '.[test]'` and `pytest tests` from this directory.

Transport recovery assumes a single controller and ordered SCPI replies. After an ambiguous exchange, the client closes the connection and uses identity replies to establish a boundary before sending ordinary queries. This also covers TCP serial bridges that retain delayed UART data across connections. Recovery waits for outstanding identity probes; if a reply is lost, operations remain unavailable rather than accepting potentially shifted measurements. Recreate the client (or reload the Home Assistant entry) after resolving a persistent recovery failure.
