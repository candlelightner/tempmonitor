import base64
import os
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    mock: bool = True
    bind: str = "127.0.0.1"
    port: int = Field(default=8090, ge=1024, le=65535)
    public_origin: str = "http://localhost:8090"
    secure_cookies: bool = True
    device_host: str = "fancontroller-r3-3-0667a0"
    device_key_file: Path = Path("secrets/esp32_api_key")
    device_mac: str = "14:63:93:06:67:A0"
    gpu_uuids: list[str] = Field(default_factory=list)
    hotspot_status: Path = Path("/run/tempmonitor-hotspot/status.json")
    retention_days: int = Field(default=7, ge=1, le=90)

    @model_validator(mode="after")
    def valid_origin(self):
        origin = urlsplit(self.public_origin)
        if (origin.scheme not in ("http", "https") or not origin.hostname or origin.username
                or origin.password or origin.path or origin.query or origin.fragment):
            raise ValueError("TM_PUBLIC_ORIGIN must be an origin, without a path or credentials")
        loopback = origin.hostname in ("localhost", "127.0.0.1", "::1")
        if origin.scheme != "https" and not loopback:
            raise ValueError("A remote public origin requires HTTPS")
        if not self.secure_cookies and not (self.mock and loopback):
            raise ValueError("Insecure cookies are only allowed for loopback mock development")
        if not self.device_host or "/" in self.device_host or ":" in self.device_host:
            raise ValueError("Device host must be a local DNS name or IPv4 address")
        return self

    @classmethod
    def from_env(cls):
        values = {}
        for name in cls.model_fields:
            value = os.getenv("TM_" + name.upper())
            if value is not None:
                values[name] = value.split(",") if name == "gpu_uuids" and value else (
                    [] if name == "gpu_uuids" else value)
        return cls.model_validate(values)

    def encryption_key(self) -> str:
        key = self.device_key_file.read_text().strip()
        try:
            if len(base64.b64decode(key, validate=True)) != 32:
                raise ValueError
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("ESPHome key must be base64 encoding of exactly 32 bytes") from exc
        return key


class Credentials(BaseModel):
    username: str = Field(default="admin", pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    password: SecretStr = Field(min_length=12, max_length=1024)


class PasswordChange(BaseModel):
    current_password: SecretStr = Field(min_length=1, max_length=1024)
    new_password: SecretStr = Field(min_length=12, max_length=1024)


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    temperature: float = Field(ge=0, le=80)
    duty: int = Field(ge=0, le=100, strict=True)


class Curve(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_duty: int = Field(default=35, ge=5, le=100, strict=True)
    points: list[Point] = Field(default_factory=lambda: [
        Point(temperature=t, duty=d) for t, d in ((35, 0), (45, 40), (60, 75), (75, 100))
    ], min_length=4, max_length=4)

    @model_validator(mode="after")
    def safe_curve(self):
        for a, b in pairwise(self.points):
            if a.temperature >= b.temperature or a.duty > b.duty:
                raise ValueError("Temperatures must increase and duty must not decrease")
        if self.points[-1].duty != 100:
            raise ValueError("The last point must reach 100% by 80°C")
        return self


def default_curve(channel: int) -> Curve:
    if channel == 4:
        return Curve(minimum_duty=25, points=[Point(temperature=t, duty=d)
                     for t, d in ((35, 0), (45, 30), (60, 50), (75, 100))])
    return Curve()


class Override(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duty: int = Field(ge=0, le=100, strict=True)
    seconds: int = Field(default=300, ge=0, le=900, strict=True)
