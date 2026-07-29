import os


def get_config():
    """Reads live from env vars (set in the Vercel dashboard) on every call -
    each serverless invocation is a fresh process anyway, so there's no
    caching benefit to avoid, and this keeps behavior in sync with settings
    changed in the dashboard without a redeploy."""
    return {
        "num_systems": int(os.environ.get("NUM_SYSTEMS", "3")),
        "worker_api_key": os.environ.get("WORKER_API_KEY", ""),
        "database_url": os.environ.get("DATABASE_URL", ""),
    }
