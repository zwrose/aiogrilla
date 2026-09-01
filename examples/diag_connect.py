# examples/diag_connect.py — manual connectivity doctor (not part of CI)
#
# Probes the AWS IoT handshake the way the library does it, reporting CONNECT and
# SUBSCRIBE separately per candidate client id. AWS IoT expresses "not authorized"
# for both steps by closing the socket (AWS_ERROR_MQTT_UNEXPECTED_HANGUP), so the
# step and candidate that fail are the diagnostic signal. As of 2026-08 the vendor
# policy authorizes exactly one client id: the UUID portion of the Cognito identity
# id (the first candidate). The legacy scheme is probed second as a canary — if it
# ever starts working again, the policy changed back.
#
# Prints no secrets (identity ids are truncated). The password is prompted
# interactively, used once, and never stored.
import asyncio
import getpass
import os

from awscrt import mqtt

from aiogrilla import GrillaClient
from aiogrilla.mqtt import _default_connection_factory

STEP_TIMEOUT_S = 20.0


def _short(s: str) -> str:
    return s[:14] + "…" if len(s) > 15 else s


async def probe(creds, client_id: str, topic: str) -> None:
    print(f"\n--- client_id = {_short(client_id)!r}")
    conn = await asyncio.to_thread(
        _default_connection_factory,
        creds=creds,
        client_id=client_id,
        on_interrupted=lambda *a, **k: print("    [interrupted]", k.get("error") or a),
        on_resumed=lambda *a, **k: None,
    )
    try:
        await asyncio.wait_for(asyncio.wrap_future(conn.connect()), STEP_TIMEOUT_S)
        print("    CONNECT   ok")
    except Exception as exc:
        print(f"    CONNECT   FAILED: {type(exc).__name__}: {exc}")
        return
    try:
        fut, _ = conn.subscribe(topic=topic, qos=mqtt.QoS.AT_LEAST_ONCE, callback=lambda **kw: None)
        suback = await asyncio.wait_for(asyncio.wrap_future(fut), STEP_TIMEOUT_S)
        print(f"    SUBSCRIBE ok: {suback}")
        # Linger to catch a delayed policy kick (AWS sometimes closes just after).
        await asyncio.sleep(5)
        print("    still connected 5s after subscribe")
    except Exception as exc:
        print(f"    SUBSCRIBE FAILED: {type(exc).__name__}: {exc}")
    finally:
        try:
            await asyncio.wait_for(asyncio.wrap_future(conn.disconnect()), STEP_TIMEOUT_S)
        except Exception:
            pass


async def main(email: str, password: str) -> None:
    client = GrillaClient()
    await client.async_login_with_password(email, password)
    grills = await client.async_get_grills()
    auth = client._auth  # noqa: SLF001 — diagnostic peeks at internals on purpose
    creds = await auth.async_iam_credentials()
    identity_id = auth.identity_id or ""
    print(f"login ok; identity={_short(identity_id)} grills={[g.id for g in grills]}")
    if not grills:
        print("no grills on account; aborting")
        return
    topic = f"grilla/{grills[0].id}/grill_state"

    iid_uuid = identity_id.split(":", 1)[-1]
    candidates = [
        iid_uuid,  # what the library uses (expected: ok)
        f"aiogrilla-{identity_id}-diag",  # legacy scheme canary (expected: hangup)
    ]
    for cid in candidates:
        await probe(creds, cid, topic)
    await client.async_disconnect()  # close the owned aiohttp session
    print("\ndone")


if __name__ == "__main__":
    _email = os.environ.get("GRILLA_EMAIL") or input("Grilla email: ")
    asyncio.run(main(_email, getpass.getpass("Grilla password: ")))
