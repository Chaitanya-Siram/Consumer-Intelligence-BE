import logging
import os
import sys
from dotenv import load_dotenv

load_dotenv()


def setup_logging(level: str | int | None = None) -> logging.Logger:
    """Configure the root logger once. Idempotent."""
    root = logging.getLogger()
    log_level = level or os.getenv("LOG_LEVEL", "INFO")
    if isinstance(log_level, str):
        log_level = getattr(logging, log_level.upper(), logging.INFO)
    root.setLevel(log_level)

    if not any(getattr(h, "_pr_solutions", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler._pr_solutions = True  # type: ignore[attr-defined]
        root.addHandler(handler)

    for noisy in ("urllib3", "httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("pr_solutions")


logger = setup_logging()

# ===========================================================================
# Configuration variables (from environment)
# ===========================================================================

class Configs:
    """Configuration variables loaded from environment variables."""

    ENVIRONMENT = os.environ.get('ENVIRONMENT', "Local")

    # LLM provider switch: "claude" (default) or "gpt"
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gpt").strip().lower()
    LLM_CONCURRENCY = int(os.getenv("LLM_CONCURRENCY", "5"))
    LLM_BATCH_SIZE = int(os.getenv("LLM_BATCH_SIZE", "20"))

    # Anthropic Claude
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")
    MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "32000"))

    # E2B sandbox (used by the chart-code agent to run LLM-generated Python)
    E2B_API_KEY = os.getenv("E2B_API_KEY", "")

    # Azure OpenAI
    AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY", "")
    AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
    AZURE_OPENAI_MODEL = os.getenv("AZURE_OPENAI_MODEL", "")

    # AWS S3 configuration
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
    AWS_REGION = os.getenv("AWS_REGION")

    AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET")
    AWS_S3_REACH_BUCKET = os.getenv("AWS_S3_REACH_BUCKET")
    AWS_S3_REACH_FILE = os.getenv("AWS_S3_REACH_FILE")
    PUBLICATION_SOURCE_FILE = os.getenv("PUBLICATION_SOURCE_FILE", "publication_cleaned_data.csv")

    SIMILAR_WEB_REST_API_KEY = os.getenv("SIMILAR_WEB_REST_API_KEY")

    # SerpAPI (Google News article fetching)
    SERP_API_KEY = os.getenv("SERP_API_KEY", "")

    # Database configuration
    DB_HOST = os.environ.get('DB_HOST')
    DB_PORT = os.environ.get('DB_PORT')
    DB_NAME = os.environ.get('DB_NAME')
    DB_USER = os.environ.get('DB_USER')
    DB_PASSWORD = os.environ.get('DB_PASSWORD')
    DB_SCHEMA = os.environ.get('DB_SCHEMA', 'pr_solution')

    # Authentication / JWT configuration
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
    JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
    REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

    # Warn about missing critical configuration variables
    required_vars = {
        "LLM_PROVIDER": LLM_PROVIDER,
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": AWS_SECRET_ACCESS_KEY,
        "AWS_S3_BUCKET": AWS_S3_BUCKET,
        "AWS_REGION": AWS_REGION,

        # DB Configs
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
    }

    for var_name, var_value in required_vars.items():
        if not var_value:
            logger.warning(f"Configuration variable '{var_name}' is not set. This may cause errors.")


envs = Configs()