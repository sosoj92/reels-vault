#!/usr/bin/env python3
"""
vault_cloud.py - Adapters cloud OPTIONNELS du Life Intelligence System (V2).

Principes :
- aucun provider cloud n'est obligatoire : sans cle, get_provider() renvoie
  None et tout le systeme continue en local (Ask deterministe, Qwen local,
  review queue en attente) ;
- les cles viennent UNIQUEMENT des variables d'environnement (processus ou
  variables Windows utilisateur), jamais du code ni de la base ;
- aucune cle n'est journalisee : les messages d'erreur passent par redact() ;
- minimisation : les appelants n'envoient que des extraits publics de sources
  (titre, description publique, extrait de transcript), jamais les notes ni
  descriptions manuelles de l'utilisateur ;
- un nouveau provider = une classe qui respecte CloudProvider, ajoutee a
  PROVIDERS. Rien d'autre ne change dans le pipeline.

Provider disponible : Claude (SDK officiel `anthropic`, modele par defaut
claude-opus-5, surchargeable par VAULT_CLAUDE_MODEL).
"""

import os
import re
from dataclasses import dataclass
from typing import Protocol


class CloudError(RuntimeError):
    """Erreur cloud deja expurgee de tout secret."""


class CloudUnavailable(CloudError):
    pass


class CloudRefusal(CloudError):
    pass


@dataclass
class CloudResponse:
    text: str
    provider: str
    model: str
    stop_reason: str | None = None
    request_id: str | None = None


class CloudProvider(Protocol):
    name: str
    model: str

    def available(self) -> tuple[bool, str | None]:
        ...

    def complete(self, system: str, user: str, *, max_tokens: int = 1024,
                 timeout: float = 30.0) -> CloudResponse:
        ...


def _env(name):
    # Meme lecture que les connecteurs du Vault : variable du processus, puis
    # variable Windows utilisateur/machine. Une variable presente mais vide
    # vaut "non configure".
    try:
        from connectors.base import env

        return env(name)
    except Exception:
        return os.environ.get(name) or None


_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(x-api-key|authorization|api[_-]?key)\s*[:=]\s*\S+"),
)


def redact(text, secrets=()):
    """Retire toute cle connue ou a forme de cle d'un message."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[secret]")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[secret]", value)
    return value


class ClaudeProvider:
    """
    Claude via le SDK officiel (`pip install anthropic`).

    - modele : VAULT_CLAUDE_MODEL, defaut claude-opus-5 ;
    - thinking adaptatif, effort "medium" (taches courtes de revue/synthese) ;
    - fallbacks serveur actives (beta server-side-fallback-2026-07-01) : si le
      modele refuse, l'API reroute la meme requete ;
    - stop_reason "refusal" -> CloudRefusal (jamais un faux resultat).
    """

    name = "claude"
    KEY_ENV = "ANTHROPIC_API_KEY"
    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(self, model=None, client_factory=None, effort=None):
        self.model = model or os.environ.get("VAULT_CLAUDE_MODEL") or "claude-opus-5"
        self.effort = effort or os.environ.get("VAULT_CLAUDE_EFFORT") or "medium"
        self._client_factory = client_factory

    def available(self):
        if not _env(self.KEY_ENV):
            return False, f"{self.KEY_ENV} absente"
        if self._client_factory is None:
            try:
                import anthropic  # noqa: F401
            except ImportError:
                return False, "paquet anthropic non installe"
        return True, None

    def _client(self, api_key, timeout):
        if self._client_factory is not None:
            return self._client_factory(api_key=api_key, timeout=timeout)
        import anthropic

        return anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=1)

    def complete(self, system, user, *, max_tokens=1024, timeout=30.0):
        ok, reason = self.available()
        if not ok:
            raise CloudUnavailable(reason)
        api_key = _env(self.KEY_ENV)
        try:
            client = self._client(api_key, timeout)
            response = client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
            )
        except CloudError:
            raise
        except Exception as exc:  # erreurs SDK : type + message expurge
            raise CloudError(
                f"{type(exc).__name__}: {redact(str(exc), (api_key,))[:300]}"
            ) from None

        if getattr(response, "stop_reason", None) == "refusal":
            raise CloudRefusal("le modele a decline la requete (refusal)")
        text = "".join(
            getattr(block, "text", "") or ""
            for block in getattr(response, "content", []) or []
            if getattr(block, "type", None) == "text"
        ).strip()
        return CloudResponse(
            text=text,
            provider=self.name,
            model=getattr(response, "model", None) or self.model,
            stop_reason=getattr(response, "stop_reason", None),
            request_id=getattr(response, "_request_id", None),
        )


# Nouvel adapter (ex. OpenAI) : ajouter une classe respectant CloudProvider.
PROVIDERS = {
    "claude": ClaudeProvider,
}


def get_provider(name=None, **kwargs):
    """
    Provider configure et disponible, ou None (jamais d'exception) :
    VAULT_CLOUD_PROVIDER choisit le provider (defaut "claude").
    """
    name = (name or os.environ.get("VAULT_CLOUD_PROVIDER") or "claude").strip().lower()
    if name in ("", "off", "none", "0"):
        return None
    factory = PROVIDERS.get(name)
    if factory is None:
        return None
    provider = factory(**kwargs)
    ok, _ = provider.available()
    return provider if ok else None


def status():
    """Etat lisible des providers, sans jamais exposer de cle."""
    out = {}
    for name, factory in PROVIDERS.items():
        provider = factory()
        ok, reason = provider.available()
        out[name] = {"available": ok, "model": provider.model, "reason": reason}
    return out


if __name__ == "__main__":
    import json

    print(json.dumps(status(), ensure_ascii=False, indent=2))
