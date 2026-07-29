import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

_DEFAULTS = {
    "num_systems": 3,
    "message_delay_seconds": 5,
    "message_template": "Hi {full_name}, thank you for registering with Motilal Oswal!",
    "country_code": "91",
    "db_path": "queue.db",
    "chrome_profile_root": "./whatsapp_profiles",
    "headless": False,
}


def get_config():
    """Reload from disk every call so num_systems / delay can be tuned without restarting."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {**_DEFAULTS, **data}
        return merged
    return dict(_DEFAULTS)
