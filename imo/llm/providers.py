"""Fournisseurs LLM interchangeables : une même interface `complete(system, prompt)`.

- claude-cli   : la commande `claude -p` et la session Claude Code de l'utilisateur (Mac perso)
- foundry      : Claude sur Microsoft Foundry (réseau de la société)
- azure-openai : modèle OpenAI (GPT) sur Microsoft Foundry / Azure OpenAI
- anthropic    : API Anthropic directe (ANTHROPIC_API_KEY)
- ollama       : modèle local servi par Ollama
- hermes       : agent Hermes (API compatible OpenAI d'un profil : HERMES_API_URL, HERMES_API_KEY)

À part, l'agent outillé des recherches (« imv recherche ») est facultatif : voir research_agent().

Sur Foundry, sans clé d'API, l'authentification passe par Microsoft Entra ID : principal de
service (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET), sinon `az login`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from typing import Protocol

from imo import metrics

log = logging.getLogger(__name__)

PROVIDERS = ("claude-cli", "foundry", "azure-openai", "anthropic", "ollama", "hermes")

DEFAULT_MODELS = {
    "claude-cli": "opus",  # alias résolu par Claude Code vers le dernier Opus
    "foundry": "$ANTHROPIC_FOUNDRY_MODEL ou claude-opus-5-5",  # nom du déploiement Foundry
    "azure-openai": "$OPENAI_MODEL",  # nom du déploiement
    "anthropic": "claude-opus-5-5",
    "ollama": "qwen3:14b",
    "hermes": "$HERMES_MODEL ou oscar",  # nom du profil Hermes
}

# Scopes des jetons Entra ID (modifiables par AZURE_TOKEN_SCOPE). La doc Claude in Microsoft
# Foundry indique ai.azure.com ; cognitiveservices est le scope historique d'Azure OpenAI.
_CLAUDE_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"


def default_model(provider: str) -> str:
    if provider == "foundry":
        return os.environ.get("ANTHROPIC_FOUNDRY_MODEL") or "claude-opus-5-5"
    if provider == "azure-openai":
        return os.environ.get("OPENAI_MODEL", "")
    if provider == "hermes":
        return os.environ.get("HERMES_MODEL") or "oscar"
    return DEFAULT_MODELS.get(provider, "")


def _foundry_endpoint() -> str | None:
    """Adresse commune de la ressource : https://<ressource>.services.ai.azure.com"""
    endpoint = os.environ.get("AZURE_FOUNDRY_ENDPOINT")
    return endpoint.rstrip("/") if endpoint else None


def _entra_token_provider(default_scope: str):
    """Jeton Entra ID renouvelé automatiquement, appelé par le SDK à chaque requête."""
    from azure.identity import DefaultAzureCredential, EnvironmentCredential, get_bearer_token_provider

    # EnvironmentCredential lit AZURE_CLIENT_ID / AZURE_TENANT_ID / AZURE_CLIENT_SECRET.
    credential = EnvironmentCredential() if os.environ.get("AZURE_CLIENT_SECRET") else DefaultAzureCredential()
    return get_bearer_token_provider(credential, os.environ.get("AZURE_TOKEN_SCOPE") or default_scope)


def _entra_error(exc: Exception) -> LLMError | None:
    from azure.core.exceptions import ClientAuthenticationError

    if isinstance(exc, ClientAuthenticationError):
        return LLMError(f"authentification Entra ID refusée : {str(exc).splitlines()[0]}")
    return None


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    async def complete(self, system: str, prompt: str) -> str: ...


class ClaudeCLI:
    """`claude -p` sans outils ni MCP : simple appel texte, décompté de l'abonnement Claude Code."""

    def __init__(self, model: str, *, executable: str = "claude", timeout: float = 120) -> None:
        self.model = model
        self.name = f"claude-cli:{model}"
        self.executable = executable
        self.timeout = timeout

    async def complete(self, system: str, prompt: str) -> str:
        args = [
            self.executable, "-p",
            "--output-format", "json",
            "--model", self.model,
            "--system-prompt", system,
            "--tools", "",
            "--no-session-persistence",
            "--strict-mcp-config",
            # Aucun réglage chargé (ni utilisateur, ni projet, ni local) : pas de hooks, sons, validateurs…
            "--setting-sources", "",
        ]
        # Dossier privé et vide : ni CLAUDE.md ni .claude/settings.json déposé par un tiers.
        with tempfile.TemporaryDirectory(prefix="imv-claude-") as workdir:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                )
            except FileNotFoundError as exc:
                raise LLMError(f"commande {self.executable!r} introuvable (Claude Code installé ?)") from exc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise LLMError(f"claude -p sans réponse après {self.timeout:.0f} s") from exc
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LLMError(f"sortie inattendue de claude -p : {(stderr or stdout)[:300]!r}") from exc
        if result.get("is_error") or result.get("subtype") != "success":
            raise LLMError(f"claude -p : {result.get('result') or result.get('subtype')}")
        return str(result.get("result", "")).strip()


class AnthropicMessages:
    """API Messages de Claude, en direct (Anthropic) ou via Microsoft Foundry."""

    def __init__(self, model: str, *, foundry: bool = False) -> None:
        import anthropic

        self.model = model
        self.foundry = foundry
        self.name = f"{'foundry' if foundry else 'anthropic'}:{model}"
        self._anthropic = anthropic
        try:
            self.client = self._foundry_client() if foundry else anthropic.AsyncAnthropic()
        except (anthropic.AnthropicError, ValueError) as exc:
            raise LLMError(f"configuration {'Foundry' if foundry else 'Anthropic'} incomplète : {exc}") from exc

    def _foundry_client(self):
        # Le SDK lit ANTHROPIC_FOUNDRY_API_KEY et ANTHROPIC_FOUNDRY_RESOURCE / _BASE_URL ;
        # on complète avec Entra ID et l'adresse commune AZURE_FOUNDRY_ENDPOINT.
        kwargs = {}
        if not os.environ.get("ANTHROPIC_FOUNDRY_API_KEY"):
            kwargs["azure_ad_token_provider"] = _entra_token_provider(_CLAUDE_FOUNDRY_SCOPE)
        endpoint = _foundry_endpoint()
        if endpoint and not (os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL") or os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")):
            kwargs["base_url"] = f"{endpoint}/anthropic/"
        return self._anthropic.AsyncAnthropicFoundry(**kwargs)

    async def complete(self, system: str, prompt: str) -> str:
        params = dict(
            model=self.model,
            max_tokens=16000,
            # Prompt système stable → mis en cache d'un appel à l'autre
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            # Réponses pendant le call : rapidité avant profondeur
            output_config={"effort": "low"},
        )
        try:
            if self.foundry:
                response = await self.client.messages.create(**params)
            else:
                # Si le modèle décline la requête, l'API la rejoue sur un modèle de repli.
                response = await self.client.beta.messages.create(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                )
        except self._anthropic.APIStatusError as exc:
            raise LLMError(f"API Claude : {exc.status_code} {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError("API Claude injoignable") from exc
        except Exception as exc:
            if (error := _entra_error(exc)) is None:
                raise
            raise error from exc
        if response.stop_reason == "refusal":
            raise LLMError("le modèle a refusé de répondre")
        return "".join(b.text for b in response.content if b.type == "text").strip()


class AzureOpenAIChat:
    """Modèle OpenAI déployé sur Microsoft Foundry / Azure OpenAI (API Chat Completions)."""

    def __init__(self, model: str) -> None:
        import openai

        self.model = model
        self.name = f"azure-openai:{model}"
        self._openai = openai
        endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT") or _foundry_endpoint()
        if not endpoint:
            raise LLMError("adresse Azure OpenAI manquante : AZURE_FOUNDRY_ENDPOINT ou AZURE_OPENAI_ENDPOINT")
        if not model:
            raise LLMError("modèle manquant : variable OPENAI_MODEL ou option --llm-model")
        key = os.environ.get("AZURE_OPENAI_API_KEY")
        auth = {"api_key": key} if key else {"azure_ad_token_provider": _entra_token_provider(_AZURE_OPENAI_SCOPE)}
        self.client = openai.AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_version=os.environ.get("OPENAI_API_VERSION") or "2024-10-21",
            **auth,
        )

    async def complete(self, system: str, prompt: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,  # nom du déploiement
                messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            )
        except self._openai.APIStatusError as exc:
            raise LLMError(f"Azure OpenAI : {exc.status_code} {exc.message}") from exc
        except self._openai.APIConnectionError as exc:
            raise LLMError("Azure OpenAI injoignable") from exc
        except Exception as exc:
            if (error := _entra_error(exc)) is None:
                raise
            raise error from exc
        return (response.choices[0].message.content or "").strip()


# Consignes ajoutées au prompt système de chaque requête à un agent outillé, selon l'usage : portées
# par la requête elle-même, elles valent quel que soit le réglage de l'agent (profil Hermes…).
HERMES_ANALYSIS_MODE = (
    "Mode analyse (consigne d'IMV, prioritaire) : réponds uniquement à partir des messages et du "
    "contexte fournis. N'utilise aucun outil : pas de recherche web, et n'enregistre rien en mémoire."
)
HERMES_TASK_MODE = """Mode tâche (consigne d'IMV, prioritaire) :
- Recherche web : 3 recherches au plus. Dans les requêtes, uniquement des termes techniques génériques \
(code ou message d'erreur public, produit, version) ; jamais un nom interne lu dans la réunion ou le \
contexte (serveur, application maison, domaine, adresse IP, client, projet, personne).
- Si tu ne trouves pas, dis-le et indique ce que tu as cherché plutôt que de prolonger.
- Mémoire : tu peux enregistrer une connaissance technique durable et générique (ex. « ORA-12541 : \
vérifier d'abord le listener »), jamais le contenu de la réunion, un nom de personne, une information \
interne à l'entreprise ni un secret.
- Cite tes sources (URL)."""


class ToolAgent:
    """Agent outillé (Hermes ou autre) joint par son API compatible OpenAI, par exemple le profil
    Hermes /p/oscar/v1 : son modèle et les outils qu'il autorise (recherche web, mémoire). `tasks` :
    client des recherches confiées (imv recherche), qui peut se servir des outils ; sinon client des
    analyses, sans outil. `label` : nom de l'agent dans les messages."""

    def __init__(
        self, model: str, *, url: str, key: str, label: str, url_variable: str, tasks: bool = False,
        timeout: float = 300,
    ) -> None:
        import openai

        self.model = model
        self.label = label
        self.url_variable = url_variable
        self.mode = HERMES_TASK_MODE if tasks else HERMES_ANALYSIS_MODE
        self.name = f"{label.lower()}:{model}"
        self._openai = openai
        self.client = openai.AsyncOpenAI(
            base_url=url,
            api_key=key,
            timeout=timeout,
            max_retries=0,  # une recherche relancée referait ses requêtes
        )

    async def complete(self, system: str, prompt: str) -> str:
        try:
            response = await self.client.chat.completions.create(
                model=self.model,  # nom du profil, annoncé par /v1/models
                messages=[
                    {"role": "system", "content": f"{system}\n\n{self.mode}"},
                    {"role": "user", "content": prompt},
                ],
            )
        except self._openai.APIStatusError as exc:
            raise LLMError(f"{self.label} : {exc.status_code} {exc.message}") from exc
        except self._openai.APITimeoutError as exc:
            raise LLMError(f"{self.label} : pas de réponse dans le délai") from exc
        except self._openai.APIConnectionError as exc:
            raise LLMError(f"{self.label} injoignable ({self.url_variable})") from exc
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        # « length » : budget d'itérations épuisé, la réponse reste utilisable
        if choice.finish_reason in ("error", "cancelled"):
            raise LLMError(f"{self.label} : tâche {choice.finish_reason}" + (f" ({text[:200]})" if text else ""))
        return text


def _hermes(model: str, *, tasks: bool) -> ToolAgent:
    key = os.environ.get("HERMES_API_KEY")
    if not key:
        raise LLMError("clé manquante : HERMES_API_KEY (API_SERVER_KEY du profil Hermes)")
    return ToolAgent(
        model,
        url=os.environ.get("HERMES_API_URL") or "http://127.0.0.1:8642/p/oscar/v1",
        key=key,
        label="Hermes",
        url_variable="HERMES_API_URL",
        tasks=tasks,
    )


def research_configured() -> bool:
    """Un agent de recherche est-il configuré (voir research_agent) ?"""
    return bool(os.environ.get("IMV_AGENT_KEY") or os.environ.get("HERMES_API_KEY"))


def research_agent() -> tuple[LLM, str] | None:
    """Agent outillé des recherches (« imv recherche »), facultatif et indépendant du LLM des
    analyses : (client, nom affiché), ou None si aucun agent n'est configuré. Agent compatible
    OpenAI quelconque par IMV_AGENT_URL, IMV_AGENT_KEY, IMV_AGENT_MODEL et IMV_AGENT_NAME ; à
    défaut, Hermes par HERMES_API_KEY (et HERMES_API_URL, HERMES_MODEL)."""
    if key := os.environ.get("IMV_AGENT_KEY"):
        url = os.environ.get("IMV_AGENT_URL")
        if not url:
            raise LLMError("adresse manquante : IMV_AGENT_URL (API compatible OpenAI de l'agent)")
        label = os.environ.get("IMV_AGENT_NAME") or "Agent"
        agent = ToolAgent(
            os.environ.get("IMV_AGENT_MODEL") or "default", url=url, key=key, label=label,
            url_variable="IMV_AGENT_URL", tasks=True,
        )
    elif research_configured():
        agent = _hermes(default_model("hermes"), tasks=True)
        label = agent.label = os.environ.get("IMV_AGENT_NAME") or "Hermes"
    else:
        return None
    return Timed(agent), label


class Ollama:
    """API native d'Ollama (/api/chat), sans dépendance supplémentaire."""

    def __init__(self, model: str, *, host: str | None = None, timeout: float = 300) -> None:
        self.model = model
        self.name = f"ollama:{model}"
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = f"http://{self.host}"
        self.timeout = timeout

    async def complete(self, system: str, prompt: str) -> str:
        request = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "stream": False,
            # Sans phase de raisonnement : ~10x plus rapide avec qwen3, suffisant pour ces tâches.
            "think": False,
        }
        try:
            data = await asyncio.to_thread(self._post, "/api/chat", json.dumps(request).encode())
        except LLMError as exc:
            if "think" not in str(exc):
                raise
            del request["think"]  # modèle qui refuse ce paramètre
            data = await asyncio.to_thread(self._post, "/api/chat", json.dumps(request).encode())
        content = data.get("message", {}).get("content", "")
        # Modèles « raisonnants » (qwen3, deepseek-r1) : le raisonnement n'est pas la réponse.
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    def _post(self, path: str, body: bytes) -> dict:
        request = urllib.request.Request(
            self.host + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise LLMError(f"Ollama : {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMError(f"Ollama injoignable sur {self.host} : {exc}") from exc


class Timed:
    """Journalise la durée de chaque appel et la compte dans les métriques (imo.metrics)."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm
        self.name = llm.name

    async def complete(self, system: str, prompt: str) -> str:
        start, ok = time.monotonic(), False
        try:
            answer = await self.llm.complete(system, prompt)
            ok = True
            return answer
        finally:
            elapsed = time.monotonic() - start
            log.info("LLM %s : %.1f s", self.name, elapsed)
            metrics.record_llm(self.name, elapsed, ok)


def make_llm(provider: str, model: str | None = None, *, hermes_tasks: bool = False) -> LLM:
    """`hermes_tasks` : client Hermes des recherches confiées (outils permis), sinon des analyses."""
    model = model or default_model(provider)
    if provider == "claude-cli":
        llm: LLM = ClaudeCLI(model)
    elif provider in ("anthropic", "foundry"):
        llm = AnthropicMessages(model, foundry=provider == "foundry")
    elif provider == "azure-openai":
        llm = AzureOpenAIChat(model)
    elif provider == "ollama":
        llm = Ollama(model)
    elif provider == "hermes":
        llm = _hermes(model, tasks=hermes_tasks)
    else:
        raise ValueError(f"Fournisseur LLM inconnu : {provider!r} (choix : {', '.join(PROVIDERS)})")
    return Timed(llm)
