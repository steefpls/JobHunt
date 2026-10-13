"""First-run import of a bundled AI key.

If a file called opencode_key.txt sits next to run.bat, JobHunt moves the key
into Windows Credential Manager (the same place a key pasted on the Accounts
page goes), switches the AI provider to OpenCode Go with its default model,
and deletes the file. The key itself is never printed, logged or put in
settings.json.
"""
from pathlib import Path

from . import paths, secrets_store
from . import settings as settings_mod

KEY_FILE = "opencode_key.txt"


def import_bundled_key(folder: Path | None = None) -> str:
    """Do the import if the file is there. Returns a message for the user ('' = nothing to do)."""
    path = Path(folder or paths.APP_DIR) / KEY_FILE
    if not path.is_file():
        return ""
    try:
        key = path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return f"Could not read {KEY_FILE}. Paste the key on the Accounts page instead."
    if len(key) < 8 or any(c.isspace() for c in key):
        return f"{KEY_FILE} does not look like an API key, so it was left alone."
    name = secrets_store.api_key_name("opencode_go")
    try:
        secrets_store.set_secret(name, key)
        stored = secrets_store.get_secret(name) == key
    except Exception:  # keyring errors could quote their input: say nothing about it
        stored = False
    if not stored:
        return (f"Could not save the AI key in Windows Credential Manager, so {KEY_FILE} was kept. "
                "Paste the key on the Accounts page instead.")
    settings_mod.update({"provider": "opencode_go", "base_url": "",
                         "models": dict(settings_mod.PROVIDER_DEFAULT_MODELS["opencode_go"])})
    try:
        path.unlink()
    except OSError:
        return f"The AI key was saved, but {KEY_FILE} could not be deleted. Please delete it yourself."
    return f"Your OpenCode Go key was moved into Windows Credential Manager and {KEY_FILE} was deleted."
