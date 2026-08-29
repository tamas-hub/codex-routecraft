"""Declared host capability registry v1, with explicit three-state evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping


CAPABILITY_REGISTRY_VERSION = "1"
KNOWN_CAPABILITIES = ("available", "native_routing", "tool_dispatch", "structured_output")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class CapabilityValidationError(ValueError):
    pass


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _OPAQUE_ID.fullmatch(value):
        raise CapabilityValidationError(f"{name} must be a bounded identifier")
    return value


def _capabilities(value: Any) -> dict[str, bool | None]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CapabilityValidationError("capabilities must be an object")
    result: dict[str, bool | None] = {}
    for name, state in value.items():
        _identifier(name, "capability")
        if state is not None and not isinstance(state, bool):
            raise CapabilityValidationError("capability state must be true, false, or null")
        result[name] = state
    return result


@dataclass(frozen=True)
class ModelCapability:
    model: str
    capabilities: Mapping[str, bool | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "capabilities": dict(self.capabilities)}


@dataclass(frozen=True)
class HostCapability:
    host: str
    capabilities: Mapping[str, bool | None] = field(default_factory=dict)
    models: tuple[ModelCapability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"host": self.host, "capabilities": dict(self.capabilities), "models": [model.to_dict() for model in self.models]}


@dataclass(frozen=True)
class ProviderCapability:
    provider: str
    capabilities: Mapping[str, bool | None] = field(default_factory=dict)
    hosts: tuple[HostCapability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "capabilities": dict(self.capabilities), "hosts": [host.to_dict() for host in self.hosts]}


class HostCapabilityRegistry:
    """Read-only capability declarations; missing information means unknown, never true."""
    schema_version = CAPABILITY_REGISTRY_VERSION

    def __init__(self, providers: tuple[ProviderCapability, ...] = ()) -> None:
        self.providers = providers

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "HostCapabilityRegistry":
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "providers"}:
            raise CapabilityValidationError("registry must contain only schema_version and providers")
        if value["schema_version"] != CAPABILITY_REGISTRY_VERSION or not isinstance(value["providers"], list):
            raise CapabilityValidationError("registry schema is invalid")
        providers: list[ProviderCapability] = []
        seen_providers: set[str] = set()
        for provider_value in value["providers"]:
            if not isinstance(provider_value, Mapping) or set(provider_value) != {"provider", "capabilities", "hosts"}:
                raise CapabilityValidationError("provider record is invalid")
            provider = _identifier(provider_value["provider"], "provider")
            if provider in seen_providers:
                raise CapabilityValidationError("provider is duplicated")
            seen_providers.add(provider)
            hosts: list[HostCapability] = []
            seen_hosts: set[str] = set()
            if not isinstance(provider_value["hosts"], list):
                raise CapabilityValidationError("provider hosts must be a list")
            for host_value in provider_value["hosts"]:
                if not isinstance(host_value, Mapping) or set(host_value) != {"host", "capabilities", "models"}:
                    raise CapabilityValidationError("host record is invalid")
                host = _identifier(host_value["host"], "host")
                if host in seen_hosts:
                    raise CapabilityValidationError("host is duplicated")
                seen_hosts.add(host)
                if not isinstance(host_value["models"], list):
                    raise CapabilityValidationError("host models must be a list")
                models: list[ModelCapability] = []
                seen_models: set[str] = set()
                for model_value in host_value["models"]:
                    if not isinstance(model_value, Mapping) or set(model_value) != {"model", "capabilities"}:
                        raise CapabilityValidationError("model record is invalid")
                    model = _identifier(model_value["model"], "model")
                    if model in seen_models:
                        raise CapabilityValidationError("model is duplicated")
                    seen_models.add(model)
                    models.append(ModelCapability(model, _capabilities(model_value["capabilities"])))
                hosts.append(HostCapability(host, _capabilities(host_value["capabilities"]), tuple(models)))
            providers.append(ProviderCapability(provider, _capabilities(provider_value["capabilities"]), tuple(hosts)))
        return cls(tuple(providers))

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "providers": [provider.to_dict() for provider in self.providers]}

    def capability(self, name: str, *, provider: str | None = None, host: str | None = None, model: str | None = None) -> bool | None:
        """Return declared state, merging only matched hierarchy; absent is unknown."""
        if provider is None:
            # A registry is evidence, not a default-provider selector.
            return None
        provider_record = next((item for item in self.providers if item.provider == provider), None)
        if provider_record is None:
            return None
        states: list[bool | None] = [provider_record.capabilities.get(name)]
        if host is not None:
            host_record = next((item for item in provider_record.hosts if item.host == host), None)
            if host_record is None:
                return None
            states.append(host_record.capabilities.get(name))
            if model is not None:
                model_record = next((item for item in host_record.models if item.model == model), None)
                if model_record is None:
                    return None
                states.append(model_record.capabilities.get(name))
        elif model is not None:
            return None
        # An unavailable parent scope makes every selected descendant unavailable.
        if name == "available" and False in states:
            return False
        for state in reversed(states):
            if state is not None:
                return state
        return None

    def model_known(self, provider: str | None, host: str | None, model: str | None) -> bool:
        if model is None:
            return True
        if provider is None or host is None:
            return False
        return any(
            (provider is None or record.provider == provider)
            and any((host is None or host_record.host == host) and any(item.model == model for item in host_record.models) for host_record in record.hosts)
            for record in self.providers
        )
