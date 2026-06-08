# examples/dump_state.py — manual live check (not part of CI)
import asyncio
import getpass
import os

from aiogrilla import GrillaClient


async def main(email: str, password: str) -> None:
    client = GrillaClient()
    # The password is used ONLY ONCE to obtain a refresh token — for repeat runs, persist
    # that token and connect with GrillaClient(refresh_token=...) instead of the password.
    await client.async_login_with_password(email, password)
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


if __name__ == "__main__":
    # Read credentials synchronously: email is not sensitive (env or prompt); the password
    # is prompted for interactively and never stored or exported.
    _email = os.environ.get("GRILLA_EMAIL") or input("Grilla email: ")
    asyncio.run(main(_email, getpass.getpass("Grilla password: ")))
