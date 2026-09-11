"""
Configuration and secrets management for Monarch Money, BigQuery, and Gemini.
Resolves secrets from GCP Secret Manager with environment variable fallback,
and parses local YAML/JSON configuration files.
"""

import functools
import json
import logging
import os

try:
    import yaml
except ImportError:
    yaml = None

try:
    from google.cloud import secretmanager
except ImportError:
    secretmanager = None

logger = logging.getLogger("monarch-gemini.config")

BQ_PROJECT_ID = os.getenv("PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", "family-finance-hub"))
BQ_DATASET_ID = os.getenv("BQ_DATASET_ID", "family_finance")

IS_PROD = bool(os.getenv("K_SERVICE"))

DEFAULT_DECOMMISSIONED_ACCOUNT_IDS: set[str] = set()
DEFAULT_ACCOUNT_OVERRIDES: dict[str, dict] = {}
DEFAULT_EXCLUDED_INSTITUTIONS: set[str] = set()


def get_chat_action_target(default_action: str = "confirm_recategorize") -> str:
    """
    Returns the target action function for Google Chat Card v2 onClick actions.
    When operating in Google Workspace Add-ons / Pub/Sub mode, Google Workspace
    requires the fully-qualified Pub/Sub topic path (projects/<project>/topics/<topic>)
    as the action function name so that GSuiteAddOns publishes the CARD_CLICKED event.
    """
    topic = os.getenv("CHAT_TOPIC", "monarch-chat-incoming")
    project = BQ_PROJECT_ID
    if project and project != "family-finance-hub":
        return f"projects/{project}/topics/{topic}"
    if os.getenv("CHAT_ACTION_USE_PUBSUB", "false").lower() in ("true", "1", "yes"):
        return f"projects/{project}/topics/{topic}"
    return default_action


@functools.lru_cache(maxsize=128)
def resolve_secret(secret_name: str, env_var: str) -> str | None:
    """Resolves secret from Secret Manager latest version, falling back to environment variable.
    Cached via LRU cache to prevent repeated Secret Manager network calls per request.
    """
    if secretmanager:
        try:
            sm_client = secretmanager.SecretManagerServiceClient()
            name = f"projects/{BQ_PROJECT_ID}/secrets/{secret_name}/versions/latest"
            resp = sm_client.access_secret_version(name=name)
            val = resp.payload.data.decode("utf-8").strip()
            if val and val != "NONE" and val != "placeholder":
                return val
        except Exception as err:
            logger.debug(f"Direct Secret Manager fetch for {secret_name} failed: {err}")

    val = os.getenv(env_var)
    if val and val != "NONE" and val != "placeholder":
        return val.strip()
    return None


def clear_secret_cache():
    """Clears the secret resolution LRU cache (useful in tests or after secret rotation)."""
    resolve_secret.cache_clear()


def load_local_config() -> dict:
    """Loads optional local configuration from config.yaml, config.yml, or config.json."""
    config_file = os.getenv("CONFIG_FILE")
    candidate_files = [config_file] if config_file else ["config.yaml", "config.yml", "config.json"]

    for file_path in candidate_files:
        if file_path and os.path.exists(file_path):
            try:
                if file_path.endswith((".yaml", ".yml")):
                    if yaml:
                        with open(file_path) as f:
                            data = yaml.safe_load(f)
                            if isinstance(data, dict):
                                return data
                    else:
                        logger.warning("PyYAML not installed; unable to parse YAML config file.")
                else:
                    with open(file_path) as f:
                        data = json.load(f)
                        if isinstance(data, dict):
                            return data
            except Exception as e:
                logger.warning(f"Failed to load config from {file_path}: {e}")
    return {}


def get_rates_config() -> dict:
    """Retrieves default interest rates from local config (YAML/JSON), Secret Manager, or ENV."""
    cfg = load_local_config().get("rates")
    if cfg and isinstance(cfg, dict):
        return dict(cfg)

    raw = resolve_secret("rates-config", "RATES_CONFIG_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to parse RATES_CONFIG_JSON: {e}")
    return {}


def get_decommissioned_account_ids() -> set[str]:
    """Retrieves set of account IDs to ignore from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("decommissioned_account_ids")
    if cfg:
        return set(cfg)

    raw = resolve_secret("decommissioned-account-ids", "DECOMMISSIONED_ACCOUNT_IDS")
    if raw:
        try:
            if raw.startswith("["):
                return set(json.loads(raw))
            return {x.strip() for x in raw.split(",") if x.strip()}
        except Exception as e:
            logger.warning(f"Failed to parse DECOMMISSIONED_ACCOUNT_IDS: {e}")
    return set(DEFAULT_DECOMMISSIONED_ACCOUNT_IDS)


def get_account_overrides() -> dict:
    """Retrieves account attribute overrides (e.g. custom APRs) from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("account_overrides")
    if cfg:
        return dict(cfg)

    raw = resolve_secret("account-overrides", "ACCOUNT_OVERRIDES_JSON")
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            logger.warning(f"Failed to parse ACCOUNT_OVERRIDES_JSON: {e}")
    return dict(DEFAULT_ACCOUNT_OVERRIDES)


def get_excluded_institutions() -> set[str]:
    """Retrieves list of institution name keywords to exclude from local config, Secret Manager, or ENV."""
    cfg = load_local_config().get("excluded_institutions")
    if cfg:
        return {str(x).strip().lower() for x in cfg if str(x).strip()}

    raw = resolve_secret("excluded-institutions", "EXCLUDED_INSTITUTIONS")
    if raw:
        return {x.strip().lower() for x in raw.split(",") if x.strip()}
    return set(DEFAULT_EXCLUDED_INSTITUTIONS)
