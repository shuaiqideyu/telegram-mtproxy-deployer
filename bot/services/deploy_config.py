from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class DeployConfig:
    num_workers: int | None = None
    tls_domain: str | None = None
    ad_tag: str | None = None
    secret_length: int = 16
    max_connections: int | None = None
    extra_sysctl: dict[str, str] = field(default_factory=dict)
    extra_docker_args: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> DeployConfig:
        if not raw:
            return cls()
        return cls(**raw)
