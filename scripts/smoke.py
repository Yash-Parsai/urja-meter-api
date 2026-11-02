"""Check the real portal end to end with the real client. Run it once before starting the API:

    python scripts/smoke.py

It logs in, fetches the signing secret, makes the signed export call, loads the transformer list,
builds the catalog and reads one meter's readings. It prints counts only, never cookies or the secret.
"""
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.catalog import CatalogService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.errors import PortalError  # noqa: E402
from app.normalize import parse_readings  # noqa: E402
from app.portal import PortalClient  # noqa: E402


def step(title: str, action):
    started = time.perf_counter()
    try:
        result = action()
    except PortalError as exc:
        print(f"  FAIL  {title}: {type(exc).__name__}: {exc}")
        sys.exit(1)
    print(f"  ok    {title} ({time.perf_counter() - started:.1f}s)")
    return result


def main() -> None:
    try:
        settings = Settings.from_env()
    except RuntimeError as exc:
        sys.exit(f"Settings problem: {exc}")

    print(f"Portal: {settings.base_url}")
    portal = PortalClient(settings)
    try:
        step("log in", portal.login)
        info = portal.session_info()
        print(f"        session valid for about {info['expires_in_seconds']} s")

        rows = step("signed export call", portal.export_meters)
        print(f"        {len(rows)} meters in the export")

        dts = step("transformer list", portal.list_transformers)
        print(f"        {len(dts)} transformers")

        catalog = CatalogService(portal, ttl_seconds=settings.catalog_ttl_seconds)
        snapshot = step("build the catalog", catalog.snapshot)
        print(f"        {len(snapshot.meters)} meters, {len(snapshot.transformers)} transformers, "
              f"{len(snapshot.notes)} data-quality notes")
        for note in snapshot.notes[:5]:
            print(f"          - {note}")

        meter = snapshot.meters[0]
        raw = step(f"readings for {meter.meter_id}", lambda: portal.read_energy(meter.meter_id))
        readings, skipped = parse_readings(raw)
        if readings:
            print(f"        {len(readings)} readings, {readings[0].timestamp:%d %b %Y %H:%M} "
                  f"to {readings[-1].timestamp:%d %b %Y %H:%M}, {skipped} skipped")
        else:
            print("        no readings returned")
    finally:
        portal.close()
    print("All checks passed.")


if __name__ == "__main__":
    main()
