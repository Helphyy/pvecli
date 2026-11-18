"""Configuration manager for pvecli."""

import contextlib
import os
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..api.exceptions import ConfigError
from ..crypto import decrypt, encrypt
from ..models.config import OutputConfig, ProfileConfig


class Config(BaseModel):
    """Main configuration model."""

    # Keep keys this version does not know, like ProfileConfig does
    model_config = ConfigDict(extra="allow")

    default_profile: str | None = None
    profiles: dict[str, ProfileConfig] = Field(default_factory=dict)
    # Named sets of profiles, used as -p @name
    groups: dict[str, list[str]] = Field(default_factory=dict)
    output: OutputConfig = Field(default_factory=OutputConfig)


class ConfigManager:
    """Manage pvecli configuration."""

    def __init__(self, config_dir: Path | None = None) -> None:
        """Initialize config manager.

        Args:
            config_dir: Custom config directory (defaults to ~/.config/pvecli)
        """
        if config_dir is None:
            config_dir = Path.home() / ".config" / "pvecli"
        self.config_dir = config_dir
        self.config_file = self.config_dir / "config.yaml"
        self._config: Config | None = None

    def _ensure_config_dir(self) -> None:
        """Create config directory if it doesn't exist."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config_dir, 0o700)

    def exists(self) -> bool:
        """Check if config file exists.

        Returns:
            True if config file exists
        """
        return self.config_file.exists()

    def load(self) -> Config:
        """Load configuration from file.

        Returns:
            Loaded configuration

        Raises:
            ConfigError: If config file doesn't exist or is invalid
        """
        if not self.exists():
            raise ConfigError(
                f"Configuration file not found at {self.config_file}. "
                "Run 'pvecli config add' to create one."
            )

        try:
            with open(self.config_file) as f:
                data = yaml.safe_load(f) or {}
            self._config = Config(**data)
            # Decrypt sensitive fields and re-encrypt plaintext on disk
            needs_save = self._decrypt_config(self._config)
            if needs_save:
                self.save(self._config)
            return self._config
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in config file: {e}")
        except Exception as e:
            raise ConfigError(f"Failed to load config: {e}")

    def save(self, config: Config) -> None:
        """Save configuration to file.

        Args:
            config: Configuration to save

        Raises:
            ConfigError: If save fails
        """
        self._ensure_config_dir()
        try:
            data = config.model_dump(exclude_none=True)
            self._encrypt_data(data)
            # Atomic: write a private temporary file, then rename it over the
            # config. Fleet mode loads the config from several threads, and a
            # load may rewrite it (plain password encrypted on first read):
            # an in-place write let another thread read a truncated file.
            fd, tmp = tempfile.mkstemp(dir=self.config_dir, prefix=".config.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    yaml.safe_dump(data, f, default_flow_style=False)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.config_file)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            self._config = config
        except Exception as e:
            raise ConfigError(f"Failed to save config: {e}")

    def get(self) -> Config:
        """Get current configuration, loading if necessary.

        Returns:
            Current configuration
        """
        if self._config is None:
            self._config = self.load()
        return self._config

    def get_profile(self, name: str | None = None) -> ProfileConfig:
        """Get a specific profile or the default.

        Args:
            name: Profile name (uses default if None)

        Returns:
            Profile configuration

        Raises:
            ConfigError: If profile not found
        """
        config = self.get()

        if name is None:
            if config.default_profile is None:
                raise ConfigError("No default profile set. Use --profile to specify one.")
            name = config.default_profile

        if name not in config.profiles:
            raise ConfigError(
                f"Profile '{name}' not found. Available profiles: "
                f"{', '.join(config.profiles.keys())}"
            )

        return config.profiles[name]

    def add_profile(self, name: str, profile: ProfileConfig) -> None:
        """Add or update a profile.

        Args:
            name: Profile name
            profile: Profile configuration
        """
        config = self.get() if self.exists() else Config()
        config.profiles[name] = profile

        if config.default_profile is None:
            config.default_profile = name

        self.save(config)

    def rename_profile(self, old: str, new: str, profile: ProfileConfig) -> None:
        """Replace profile `old` by `new` in one write.

        The default profile and the groups follow the rename: a command run
        without -p must keep targeting the same cluster, and `-p @group`
        must not point at a profile that no longer exists.
        """
        config = self.get()
        if old not in config.profiles:
            raise ConfigError(f"Profile '{old}' not found")
        if new != old and new in config.profiles:
            raise ConfigError(f"Profile '{new}' already exists")
        config.profiles = {
            (new if k == old else k): (profile if k == old else v) for k, v in config.profiles.items()
        }
        if config.default_profile == old:
            config.default_profile = new
        config.groups = {g: [new if m == old else m for m in members] for g, members in config.groups.items()}
        self.save(config)

    def remove_profile(self, name: str) -> None:
        """Remove a profile.

        Args:
            name: Profile name

        Raises:
            ConfigError: If profile not found
        """
        config = self.get()

        if name not in config.profiles:
            raise ConfigError(f"Profile '{name}' not found")

        del config.profiles[name]

        if config.default_profile == name:
            config.default_profile = next(iter(config.profiles.keys()), None)

        self.save(config)

    def set_default_profile(self, name: str) -> None:
        """Set the default profile.

        Args:
            name: Profile name

        Raises:
            ConfigError: If profile not found
        """
        config = self.get()

        if name not in config.profiles:
            raise ConfigError(f"Profile '{name}' not found")

        config.default_profile = name
        self.save(config)

    def list_profiles(self) -> list[str]:
        """List all profile names.

        Returns:
            List of profile names
        """
        config = self.get()
        return list(config.profiles.keys())

    @staticmethod
    def _decrypt_config(config: Config) -> bool:
        """Decrypt sensitive fields in-place. Returns True if plaintext was found (needs re-save)."""
        from ..crypto import AGE_PREFIX

        needs_save = False
        for profile in config.profiles.values():
            auth = profile.auth
            if auth.password:
                if auth.password.startswith(AGE_PREFIX):
                    auth.password = decrypt(auth.password)
                else:
                    needs_save = True
            if auth.token_value:
                if auth.token_value.startswith(AGE_PREFIX):
                    auth.token_value = decrypt(auth.token_value)
                else:
                    needs_save = True
        return needs_save

    @staticmethod
    def _encrypt_data(data: dict) -> None:
        """Encrypt sensitive fields in the serialized dict before writing."""
        for profile in data.get("profiles", {}).values():
            auth = profile.get("auth", {})
            if auth.get("password"):
                auth["password"] = encrypt(auth["password"])
            if auth.get("token_value"):
                auth["token_value"] = encrypt(auth["token_value"])
