import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

_DEFAULTS = {
    "api_base": "http://localhost:5000",
    "worker_api_key": "",
    "message_delay_seconds": 5,
    "message_template": "Hi {first_name}, thank you for registering with Motilal Oswal!",
    "message_image": None,
    "country_code": "91",
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
