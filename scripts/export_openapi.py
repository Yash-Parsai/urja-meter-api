"""Write openapi.json from the running app's own description.  Run:  python scripts/export_openapi.py"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

spec = create_app().openapi()
(ROOT / "openapi.json").write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"wrote openapi.json ({len(spec['paths'])} paths, {len(spec['components']['schemas'])} schemas)")
