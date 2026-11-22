from dataclasses import dataclass
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = ROOT / '.env') -> None:
    """Простой .env без подстановки команд и сторонних библиотек."""
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, sep, value = line.partition('=')
            if sep and key.strip():
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


@dataclass(frozen=True)
class Config:
    vault: Path
    allowed_user: str
    token: str = ''
    model: str = 'qwen3:4b'
    model_url: str = 'http://127.0.0.1:11434'
    model_timeout: int = 300

    @classmethod
    def read(cls, *, demo: bool = False, vault: str | None = None):
        load_env()
        path = Path(vault or ('vault-demo' if demo else os.getenv('VAULT_PATH', 'vault')))
        return cls(
            vault=path if path.is_absolute() else ROOT / path,
            allowed_user='demo-student' if demo else os.getenv('ALLOWED_USER_ID', ''),
            token=os.getenv('TELEGRAM_BOT_TOKEN', ''),
            model=os.getenv('OLLAMA_MODEL', 'qwen3:4b'),
            model_url=os.getenv('OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/'),
            model_timeout=int(os.getenv('MODEL_TIMEOUT', '300')),
        )
