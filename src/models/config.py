"""Configuration models."""

from typing import Any

from pydantic import BaseModel, Field, field_validator


class AuthConfig(BaseModel):
    """Authentication configuration."""

    type: str = Field(..., pattern="^(token|password)$")
    user: str
    token_name: str | None = None
    token_value: str | None = None
    password: str | None = None

    @field_validator("token_name", "token_value")
    @classmethod
    def validate_token_fields(cls, v: str | None, info: Any) -> str | None:
        """Validate token fields are present when auth type is token.

        Args:
            v: Field value
            info: Validation info

        Returns:
            Validated value

        Raises:
            ValueError: If token fields missing when type is token
        """
        if info.data.get("type") == "token" and v is None:
            raise ValueError(f"{info.field_name} required when auth type is 'token'")
        return v


class ProfileConfig(BaseModel):
    """Profile configuration for a Proxmox cluster."""

    host: str
    port: int = 8006
    verify_ssl: bool = True
    auth: AuthConfig
    timeout: int = 30
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_key: str | None = None
    rdp_user: str | None = None
    rdp_port: int = 3389


class OutputConfig(BaseModel):
    """Output preferences."""

    format: str = Field(default="table", pattern="^(table|json|yaml)$")
    colors: bool = True
    confirm_destructive: bool = True
