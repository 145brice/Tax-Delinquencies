"""Import the actual application without reading .env or touching real data."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def isolated_app(test):
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    system_env = {key: value for key, value in os.environ.items()
                  if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC"}}
    env = patch.dict(os.environ, {**system_env, "SECRET_KEY": "isolated-test-secret", "SQLITE_DB": str(Path(tmp.name) / "state.sqlite3"),
        "DISABLE_STARTUP_MIGRATION": "1", "DISABLE_FULFILLMENT_WORKER": "1"}, clear=True)
    env.start()
    test.addCleanup(env.stop)
    dot = patch("dotenv.load_dotenv")
    dot.start()
    test.addCleanup(dot.stop)
    name = "isolated_foreclosure_app_" + str(id(test))
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    test.addCleanup(sys.modules.pop, name, None)
    spec.loader.exec_module(module)
    module.app.testing = True
    module.DATA_DIR = tmp.name
    module.DATA_FILE = str(Path(tmp.name) / "listings.json")
    module.STOREFRONT_CSV = str(Path(tmp.name) / "storefront_listings.csv")
    module.SETTINGS_FILE = str(Path(tmp.name) / "settings.json")
    module._sqlite_set("listings", [])
    return module
