"""Endpoint configuration for any OpenAI-compatible server.

Resolution order (first wins):
    1. explicit CLI flags  (--base-url / --api-key / --model)
    2. environment         (VHARNESS_* with OPENAI_* fallbacks)
    3. TOML config profile (--profile <name> section)
    4. TOML config [default] section

Config files (first found wins):
    --config, $VHARNESS_CONFIG, ~/.config/vharness/config.toml,
    ./vharness.toml (project-local), ./vharness.config.toml

A project-local file found by searching the current directory may set `model`
only. Its `base_url` and `api_key` are ignored, because a repository you scan
must not choose where your API key and code are sent. To use such a file in
full, name it with --config or $VHARNESS_CONFIG.

Config file shape:

    # ~/.config/vharness/config.toml
    [default]
    base_url = "http://localhost:11434/v1"
    model = "qwen2.5-coder:7b"
    # api_key = "sk-..."            # local servers usually don't need one

    [openrouter]
    base_url = "https://openrouter.ai/api/v1"
    api_key = "sk-or-v1-..."
    model = "mistralai/mistral-small"

    [vllm]
    base_url = "http://localhost:8000/v1"
    model = "my-finetune"

Then: `--profile openrouter`, or rely on [default] when no profile is named.
API keys can live in the file or be overridden by env/flag — put secrets in
the user-level file (chmod 600), not in a committed project file.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .log import log

if sys.version_info >= (3, 11):
    import tomllib
else:  # py3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover — bare 3.10 without tomli
        tomllib = None


class ConfigError(RuntimeError):
    pass


#: User-level config location (also settable via $VHARNESS_CONFIG).
USER_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vharness" / "config.toml"
PROJECT_CONFIGS = ("vharness.toml", "vharness.config.toml")

# Keys accepted in each profile section.
_SECTION_KEYS = ("base_url", "api_key", "model")
# Keys that decide where requests and the API key go.
_ENDPOINT_KEYS = ("base_url", "api_key")


@dataclass
class EndpointConfig:
    base_url: str
    api_key: str
    model: str

    def describe(self) -> str:
        key = self.api_key[:4] + "…" if self.api_key else "(none)"
        return f"{self.base_url} model={self.model} key={key}"


@dataclass(frozen=True)
class ConfigFile:
    """A located config file and how it was found."""

    path: Path
    #: True when found by searching the current directory rather than named
    #: by the user (flag, $VHARNESS_CONFIG) or at the user-level location.
    discovered_in_project: bool


def _load_toml(path: str | Path) -> dict:
    if tomllib is None:
        return {}
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except OSError as e:  # e.g. permission denied — visible, not fatal
        log.warning("ignoring unreadable config %s: %s", path, e)
        return {}
    except ValueError as e:  # malformed TOML or bad encoding — visible, not fatal
        log.warning("ignoring malformed config %s: %s", path, e)
        return {}


def locate_config(explicit: str | None = None) -> ConfigFile | None:
    """First existing config: explicit, $VHARNESS_CONFIG, user, project-local."""
    named: list[Path] = []
    if explicit:
        named.append(Path(explicit))
    env_path = os.environ.get("VHARNESS_CONFIG")
    if env_path and Path(env_path) not in named:
        named.append(Path(env_path))
    named.append(Path(USER_CONFIG))
    for candidate in named:
        if candidate.is_file():
            return ConfigFile(path=candidate, discovered_in_project=False)
    for name in PROJECT_CONFIGS:
        candidate = Path(name)
        if candidate.is_file():
            return ConfigFile(path=candidate, discovered_in_project=True)
    return None


def find_config_file(explicit: str | None = None) -> Path | None:
    """Path of the config file `locate_config` would use, if any."""
    found = locate_config(explicit)
    return found.path if found else None


def _restrict_project_section(cfg: ConfigFile, section: dict) -> dict:
    """Drop endpoint keys from a config discovered in the current directory."""
    if not cfg.discovered_in_project:
        return section
    blocked = [k for k in _ENDPOINT_KEYS if k in section]
    if blocked:
        log.warning(
            "ignoring %s from project-local config %s: a scanned repository may not "
            "choose the endpoint or key; name the file with --config or "
            "VHARNESS_CONFIG to trust it",
            ", ".join(blocked),
            cfg.path,
        )
    return {k: v for k, v in section.items() if k not in _ENDPOINT_KEYS}


def _is_project_local(path: Path) -> bool:
    """True for a file under the current directory other than the user config."""
    resolved = path.resolve()
    if resolved == Path(USER_CONFIG).resolve():
        return False
    return resolved.is_relative_to(Path.cwd().resolve())


def load_profile(data: dict, profile: str | None) -> dict:
    """Merge [default] with the named profile (profile wins)."""
    merged: dict = {}
    base = data.get("default")
    if isinstance(base, dict):
        merged.update({k: base[k] for k in _SECTION_KEYS if k in base})
    if profile:
        section = data.get(profile)
        if not isinstance(section, dict):
            known = [k for k, v in data.items() if isinstance(v, dict) and k != "default"]
            raise ConfigError(
                f"config profile {profile!r} not found; available profiles: {known or 'none'}"
            )
        merged.update({k: section[k] for k in _SECTION_KEYS if k in section})
    return merged


def resolve_endpoint(
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    *,
    env_prefix: str = "VHARNESS",
    profile: str | None = None,
    config_file: str | None = None,
) -> tuple[EndpointConfig, str]:
    """Resolve endpoint settings; returns (config, description-of-source)."""
    # 1) CLI flags — passed in non-None.
    # 2) environment.
    base_url = base_url or os.environ.get(f"{env_prefix}_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
    api_key = api_key or os.environ.get(f"{env_prefix}_API_KEY") or os.environ.get("OPENAI_API_KEY")
    model = model or os.environ.get(f"{env_prefix}_MODEL") or os.environ.get("OPENAI_MODEL")

    # 3/4) config file profile (only fills gaps left by flags/env).
    config_src = "flags/env"
    if not (base_url and api_key is not None and model):
        found = locate_config(config_file)
        if found is not None:
            cfg_path = found.path
            data = _load_toml(cfg_path)
            section = _restrict_project_section(found, load_profile(data, profile))
            from_file: list[str] = []
            if not base_url and "base_url" in section:
                base_url = section["base_url"]
                config_src = f"config:{cfg_path}"
                from_file.append("base_url")
            if not api_key and "api_key" in section:
                api_key = section["api_key"]
                from_file.append("api_key")
            if not model:
                model = section.get("model")
            if profile and not any(k in section for k in _SECTION_KEYS):
                config_src = f"config:{cfg_path}[{profile}]"
            if from_file and _is_project_local(cfg_path):
                log.warning(
                    "using %s from project-local config %s",
                    ", ".join(from_file),
                    cfg_path,
                )

    if not model:
        raise ConfigError(
            "no model name: pass --model, set VHARNESS_MODEL, or add `model` to "
            f"{'profile ' + profile + ' in ' if profile else ''}a config file ({USER_CONFIG} or ./vharness.toml)"
        )
    if not base_url:
        raise ConfigError(
            "no endpoint: pass --base-url, set VHARNESS_BASE_URL, or add `base_url` to "
            f"{'profile ' + profile + ' in ' if profile else ''}a config file "
            "(any OpenAI-compatible server: Ollama, vLLM, llama.cpp, LM Studio, ...)"
        )
    if not api_key:
        # Many local servers accept any key; default rather than fail.
        api_key = "no-key"
    return EndpointConfig(base_url=base_url.rstrip("/"), api_key=api_key, model=model), config_src


# Backwards-compatible single-return helper (used by tests / external callers).
def resolve_endpoint_config(
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    **kwargs,
) -> EndpointConfig:
    cfg, _src = resolve_endpoint(base_url, api_key, model, **kwargs)
    return cfg
