"""
api/llm_client.py
=================
LLM Abstraction Layer — V-CORE v1.5

Capa central de independencia tecnologica. Todo modelo se invoca a traves
de esta capa. Nunca directo desde los agentes. Sin excepciones. Sin hardcode.

Componentes:
  - LLMClient(ABC)        interfaz uniforme complete() / stream() / embed()
  - GeminiClient          Google Gemini via google-genai
  - DeepSeekClient        DeepSeek via OpenAI-compatible API
  - AnthropicClient       Claude via anthropic SDK
  - OllamaClient          modelos locales via Ollama HTTP
  - CircuitBreaker        3 estados: CLOSED / OPEN / HALF-OPEN
  - RateLimiter           cola + backoff exponencial por provider
  - LLMRouter             factory config-driven via model_routing.yaml
  - CouncilOrchestrator   N providers en paralelo, metrica de divergencia
  - UsageTracker          registro en llm_usage_log (vcore.db)

Uso:
    from api.llm_client import get_router

    router = get_router()
    response = await router.complete(role="orchestrator_lead", messages=[...])
    response = await router.complete(role="planner_plan", messages=[...], system="...")

    # Council mode
    result = await router.council(
        roles=["orchestrator_lead", "orchestrator_council"],
        messages=[...]
    )
    if result.diverged:
        # mostrar ambas respuestas al usuario
        pass
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import sqlite3
import hashlib
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Optional
import yaml


# =============================================================================
# SCHEMAS DE RESPUESTA
# =============================================================================

@dataclass
class LLMResponse:
    """Respuesta unificada de cualquier provider."""
    content: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    estimated_cost: float = 0.0
    duration_seconds: float = 0.0
    raw: Any = None  # respuesta nativa del provider para debug
    tool_calls: list[dict] | None = None  # native function calling (OpenAI format)


@dataclass
class StreamChunk:
    """Chunk de streaming con tipo — para diferenciar texto de tool calls.

    `reasoning` es un tipo propio a propósito: los modelos de razonamiento
    (GLM 5.3, Kimi K3, Nemotron 3) emiten el texto visible en `content` **solo
    cuando terminan de razonar**, y hasta entonces todo va en
    `reasoning_content`. Si se mezclan en el mismo flujo, el usuario ve el
    monólogo interno como si fuera la respuesta.
    """
    type: str  # "text" | "reasoning" | "tool_call" | "done"
    content: str = ""
    tool_calls: list[dict] | None = None  #Solo en type="tool_call"
    tokens_in: int = 0
    tokens_out: int = 0
    finish_reason: str = ""  # "stop" / "tool_calls" / "length"


@dataclass
class CouncilResult:
    """Resultado de council mode (N providers en paralelo)."""
    responses: list[LLMResponse]
    diverged: bool
    divergence_score: float  # 0.0 = identicos, 1.0 = completamente distintos
    consensus: Optional[str] = None  # si no divergen, la respuesta comun


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

class CircuitBreaker:
    """
    3 estados: CLOSED (normal) -> OPEN (corte) -> HALF-OPEN (recovery)

    Diferenciacion critica de errores:
      - 429 (rate limit): NO cuenta para el error rate — limpia en segundos
      - 5xx (server error): SI cuenta — puede durar horas
      - 401/403 (auth): corte inmediato, requiere intervencion humana
    """

    TRANSIENT_CODES  = {429}
    PERSISTENT_CODES = {500, 502, 503, 504}
    FATAL_CODES      = {401, 403}

    def __init__(
        self,
        provider: str,
        error_threshold: float = 0.30,
        window_seconds: int = 60,
        recovery_timeout: int = 120,
        min_calls_threshold: int = 5,
    ):
        self.provider = provider
        self.error_threshold = error_threshold
        self.window_seconds = window_seconds
        self.recovery_timeout = recovery_timeout
        self.min_calls_threshold = min_calls_threshold

        self.state = "CLOSED"
        self._error_times: deque[float] = deque()
        self._total_calls: int = 0
        self._recovery_at: float = 0.0
        self._consecutive_council_divergences: int = 0

    def allow_request(self) -> bool:
        """True si el circuit breaker permite el request."""
        now = time.time()

        if self.state == "CLOSED":
            return True

        if self.state == "OPEN":
            if now >= self._recovery_at:
                self._transition("HALF-OPEN", "recovery_timeout_elapsed")
                return True  # permitir el request de sondeo
            return False

        if self.state == "HALF-OPEN":
            return True  # un request de sondeo

        return False

    def record_success(self) -> None:
        self._total_calls += 1
        if self.state == "HALF-OPEN":
            self._transition("CLOSED", "half_open_success")

    def record_error(self, status_code: int) -> None:
        self._total_calls += 1

        if status_code in self.FATAL_CODES:
            self._transition("OPEN", f"fatal_error_{status_code}")
            return

        if status_code in self.TRANSIENT_CODES:
            return  # 429 no cuenta para el circuit breaker

        if status_code in self.PERSISTENT_CODES:
            now = time.time()
            self._error_times.append(now)
            self._prune_window(now)

            # No evaluar hasta tener suficientes calls (evita false positives en cold start)
            if self._total_calls < self.min_calls_threshold:
                return

            error_rate = len(self._error_times) / max(self._total_calls, 1)
            if error_rate > self.error_threshold:
                self._transition(
                    "OPEN",
                    f"error_rate_{error_rate:.2%}_exceeded_threshold"
                )
            elif self.state == "HALF-OPEN":
                self._transition("OPEN", "half_open_failure")

    def record_council_divergence(self) -> None:
        """
        Si el council detecta divergencia persistente (3 seguidas),
        bloquear para revision humana.
        """
        self._consecutive_council_divergences += 1
        if self._consecutive_council_divergences >= 3:
            self._transition(
                "OPEN",
                "council_persistent_divergence_requires_human_review"
            )

    def record_council_consensus(self) -> None:
        self._consecutive_council_divergences = 0

    def _transition(self, new_state: str, reason: str) -> None:
        old_state = self.state
        self.state = new_state
        if new_state == "OPEN":
            self._recovery_at = time.time() + self.recovery_timeout
        print(
            f"[CircuitBreaker][{self.provider}] "
            f"{old_state} -> {new_state} | reason: {reason}"
        )
        # Log a DB si esta disponible
        _log_circuit_transition(self.provider, old_state, new_state, reason)

    def _prune_window(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._error_times and self._error_times[0] < cutoff:
            self._error_times.popleft()


# =============================================================================
# RATE LIMITER
# =============================================================================

class RateLimiter:
    """
    Cola con backoff exponencial por provider.
    Respeta limites de RPM (requests per minute) configurados.
    429 dispara backoff — no el circuit breaker.
    """

    def __init__(self, provider: str, rpm_limit: int = 15):
        self.provider = provider
        self.rpm_limit = rpm_limit
        self._request_times: deque[float] = deque()
        self._backoff_until: float = 0.0
        self._backoff_multiplier: float = 1.0

    async def acquire(self) -> None:
        """Esperar si es necesario antes de enviar un request."""
        now = time.time()

        # Respetar backoff activo (por 429)
        if now < self._backoff_until:
            wait = self._backoff_until - now
            print(f"[RateLimiter][{self.provider}] backoff {wait:.1f}s")
            await asyncio.sleep(wait)

        # Respetar RPM
        self._prune_window(time.time())
        if len(self._request_times) >= self.rpm_limit:
            oldest = self._request_times[0]
            wait = 60.0 - (time.time() - oldest) + 0.1
            if wait > 0:
                print(f"[RateLimiter][{self.provider}] RPM limit, wait {wait:.1f}s")
                await asyncio.sleep(wait)

        self._request_times.append(time.time())

    def record_429(self) -> None:
        """Backoff exponencial en 429."""
        self._backoff_multiplier = min(self._backoff_multiplier * 2, 64)
        self._backoff_until = time.time() + self._backoff_multiplier
        print(
            f"[RateLimiter][{self.provider}] "
            f"429 received, backoff {self._backoff_multiplier}s"
        )

    def record_success(self) -> None:
        """Reset backoff en exito."""
        self._backoff_multiplier = 1.0

    def _prune_window(self, now: float) -> None:
        cutoff = now - 60.0
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()


# =============================================================================
# BASE CLIENT
# =============================================================================

class LLMClient(ABC):
    """Interfaz uniforme para todos los providers."""

    @abstractmethod
    async def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        """Completion basico — retorna LLMResponse."""
        pass

    @abstractmethod
    async def stream(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming — yields chunks de texto."""
        pass

    async def stream_complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncGenerator["StreamChunk", None]:
        """
        Streaming con tipos — yields StreamChunk(text/tool_call/done).
        Default: wraps legacy stream() into text chunks.
        Subclasses (DeepSeekClient) override for native tool streaming.
        """
        from api.llm_client import StreamChunk
        # Try passing tools/tool_choice — some clients (OllamaClient) don't accept them
        try:
            async for chunk in self.stream(
                model, messages, system, max_tokens, temperature, tools, tool_choice
            ):
                yield StreamChunk(type="text", content=chunk)
        except TypeError:
            # Client.stream() doesn't accept tools/tool_choice — retry without
            async for chunk in self.stream(
                model, messages, system, max_tokens, temperature
            ):
                yield StreamChunk(type="text", content=chunk)
        yield StreamChunk(type="done")


# =============================================================================
# GEMINI CLIENT
# =============================================================================

class GeminiClient(LLMClient):
    """Google Gemini via google-genai SDK."""

    # Costo estimado USD por millon de tokens (Gemini 2.5 Flash free tier = $0)
    COST_PER_M_IN  = 0.00
    COST_PER_M_OUT = 0.00

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        start = time.time()

        def _sync_call():
            from google.genai import types
            client = self._get_client()

            # DEBUG TEMPORAL
            import os as _os
            print(f"[GEMINI-DEBUG] api_key set: {bool(self.api_key)} len={len(self.api_key)} prefix={self.api_key[:12] if self.api_key else 'EMPTY'}", flush=True)
            print(f"[GEMINI-DEBUG] model={model} max_tokens={max_tokens}", flush=True)

            # Convertir messages a formato Gemini
            contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part(text=msg["content"])]
                    )
                )

            config = types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
                system_instruction=system if system else None,
            )

            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            return response

        loop = asyncio.get_event_loop()
        response = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_call),
            timeout=30.0  # timeout 30s para evitar cuelgues (ver issue Ollama)
        )

        text = response.text or ""
        tokens_in  = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        tokens_out = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        cost = (tokens_in / 1_000_000 * self.COST_PER_M_IN +
                tokens_out / 1_000_000 * self.COST_PER_M_OUT)

        return LLMResponse(
            content=text,
            provider="gemini",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            estimated_cost=cost,
            duration_seconds=round(time.time() - start, 2),
            raw=response,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        def _sync_stream():
            from google.genai import types
            client = self._get_client()

            contents = []
            for msg in messages:
                role = "user" if msg["role"] == "user" else "model"
                contents.append(
                    types.Content(
                        role=role,
                        parts=[types.Part(text=msg["content"])]
                    )
                )

            config = types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                temperature=temperature,
                system_instruction=system if system else None,
            )

            return client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )

        loop = asyncio.get_event_loop()
        stream_iter = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_stream),
            timeout=30.0
        )

        for chunk in stream_iter:
            if chunk.text:
                yield chunk.text


# =============================================================================
# DEEPSEEK CLIENT (OpenAI-compatible)
# =============================================================================

class DeepSeekClient(LLMClient):
    """DeepSeek via OpenAI-compatible API."""

    BASE_URL = "https://api.deepseek.com"
    COST_PER_M_IN  = 0.35
    COST_PER_M_OUT = 0.85

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.BASE_URL,
            )
        return self._client

    async def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        start = time.time()
        client = self._get_client()

        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        kwargs = dict(
            model=model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        response = await client.chat.completions.create(**kwargs)

        text = response.choices[0].message.content or ""
        tokens_in  = response.usage.prompt_tokens if response.usage else 0
        tokens_out = response.usage.completion_tokens if response.usage else 0
        cost = (tokens_in / 1_000_000 * self.COST_PER_M_IN +
                tokens_out / 1_000_000 * self.COST_PER_M_OUT)

        # Extract native tool calls if present
        native_tool_calls = None
        if hasattr(response.choices[0].message, 'tool_calls') and response.choices[0].message.tool_calls:
            native_tool_calls = []
            for tc in response.choices[0].message.tool_calls:
                native_tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        provider = getattr(self, 'PROVIDER_NAME', 'deepseek')

        return LLMResponse(
            content=text,
            provider=provider,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            estimated_cost=cost,
            duration_seconds=round(time.time() - start, 2),
            raw=response,
            tool_calls=native_tool_calls,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        client = self._get_client()

        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        stream = await client.chat.completions.create(
            model=model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def stream_complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Streaming con soporte para tool calls.
        Retorna StreamChunks tipados (text/tool_call/done).
        Esto es lo que usa LLMRouter.stream_complete().
        """
        from api.llm_client import StreamChunk
        client = self._get_client()

        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)

        kwargs = dict(
            model=model,
            messages=msgs,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        stream = await client.chat.completions.create(**kwargs)

        # Acumular tool calls a medida que llegan fragments
        pending_tool_calls: dict[int, dict] = {}  # index -> {id, name, arguments_str}
        tokens_in = 0

        async for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]

            # Token usage (solo disponible en el último chunk con stream_options)
            if hasattr(chunk, 'usage') and chunk.usage:
                tokens_in = getattr(chunk.usage, 'prompt_tokens', 0) or tokens_in

            delta = choice.delta

            # Texto — emitir inmediatamente
            if delta.content:
                yield StreamChunk(type="text", content=delta.content)

            # Razonamiento — canal separado.
            #
            # Los modelos de razonamiento del catálogo actual (GLM 5.3, Kimi K3,
            # Nemotron 3, gpt-oss) mandan la cadena de pensamiento en
            # `reasoning_content` y dejan `content` en null durante toda esa
            # fase. Sin esta rama, el delta se descartaba y el agente respondía
            # **vacío**: no era un problema de un modelo, era que el cliente no
            # conocía el campo. El SDK lo expone en `model_extra` porque no
            # forma parte del esquema estándar de OpenAI.
            reasoning = None
            if hasattr(delta, "model_extra") and delta.model_extra:
                reasoning = delta.model_extra.get("reasoning_content")
            if not reasoning and hasattr(delta, "reasoning_content"):
                reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamChunk(type="reasoning", content=reasoning)

            # Tool call deltas — acumular
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in pending_tool_calls:
                        pending_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments_str": "",
                        }
                    if tc_delta.id:
                        pending_tool_calls[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            pending_tool_calls[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            pending_tool_calls[idx]["arguments_str"] += tc_delta.function.arguments

            # Finish reason — emitir resultado final
            if choice.finish_reason:
                if choice.finish_reason == "tool_calls" and pending_tool_calls:
                    # Construir tool calls completos
                    complete_tcs = []
                    for idx in sorted(pending_tool_calls.keys()):
                        tc = pending_tool_calls[idx]
                        complete_tcs.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments_str"],
                            },
                        })
                    yield StreamChunk(
                        type="tool_call",
                        tool_calls=complete_tcs,
                        finish_reason=choice.finish_reason,
                    )
                else:
                    yield StreamChunk(
                        type="done",
                        finish_reason=choice.finish_reason,
                        tokens_in=tokens_in,
                    )
                return

        # Si el stream termina sin finish_reason explícito
        if pending_tool_calls:
            complete_tcs = []
            for idx in sorted(pending_tool_calls.keys()):
                tc = pending_tool_calls[idx]
                complete_tcs.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc["arguments_str"],
                    },
                })
            yield StreamChunk(type="tool_call", tool_calls=complete_tcs)
        else:
            yield StreamChunk(type="done", tokens_in=tokens_in)


# =============================================================================
# ANTHROPIC CLIENT
# =============================================================================

class AnthropicClient(LLMClient):
    """Claude via anthropic SDK — fallback externo / revisor."""

    COST_PER_M_IN  = 3.00
    COST_PER_M_OUT = 15.00

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        start = time.time()
        client = self._get_client()

        kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        response = await client.messages.create(**kwargs)

        text = response.content[0].text if response.content else ""
        tokens_in  = response.usage.input_tokens if response.usage else 0
        tokens_out = response.usage.output_tokens if response.usage else 0
        cost = (tokens_in / 1_000_000 * self.COST_PER_M_IN +
                tokens_out / 1_000_000 * self.COST_PER_M_OUT)

        return LLMResponse(
            content=text,
            provider="anthropic",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            estimated_cost=cost,
            duration_seconds=round(time.time() - start, 2),
            raw=response,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        client = self._get_client()

        kwargs = dict(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if system:
            kwargs["system"] = system

        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text


# =============================================================================
# OLLAMA CLIENT (local)
# =============================================================================

class OllamaClient(LLMClient):
    """Modelos locales via Ollama HTTP API. $0, privado."""

    def __init__(self, host: str = "http://127.0.0.1:11434"):
        self.host = host

    def _build_prompt(self, messages: list[dict], system: str = "") -> str:
        """
        Construye el prompt en formato ChatML para /api/generate.
        Los modelos Ollama locales (ej: harmonic-hermes, qwen) usan Modelfile templates
        que solo se activan con /api/generate — /api/chat ignora el template.
        
        B8-3: Filtra role:tool y tool_calls que Ollama no soporta.
        Convierte role:tool → role:user con prefijo [Tool result].
        """
        parts = []
        if system:
            parts.append(f"<|im_start|>system\n{system}<|im_end|>")
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            
            # B8-3: Ollama no soporta role:tool ni tool_calls
            if role == "tool":
                # Convertir a user message con prefijo claro
                tool_name = msg.get("name", "tool")
                parts.append(f"<|im_start|>user\n[Tool result — {tool_name}]: {content}<|im_end|>")
                continue
            if role == "system":
                continue  # system ya fue inyectado arriba
            # Si el mensaje tiene tool_calls (assistant con function calling), 
            # convertir a texto plano
            if msg.get("tool_calls"):
                tc_texts = []
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "unknown")
                    args = fn.get("arguments", "{}")
                    tc_texts.append(f"Calling {name}({args})")
                combined = content + "\n" + "\n".join(tc_texts) if content else "\n".join(tc_texts)
                parts.append(f"<|im_start|>assistant\n{combined}<|im_end|>")
                continue
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append("<|im_start|>assistant")
        return "\n".join(parts)

    async def complete(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        """
        Usa /api/generate con prompt ChatML construido manualmente.
        Los modelos Ollama locales con Modelfile templates (ej: {{ .System }}/{{ .Prompt }})
        solo funcionan con /api/generate. /api/chat bypasea el template y
        el system prompt nunca llega al modelo — Orchestrator pierde identidad.
        """
        import aiohttp
        start = time.time()

        prompt = self._build_prompt(messages, system)

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.host}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()

        text = data.get("response", "")
        tokens_in  = data.get("prompt_eval_count", 0)
        tokens_out = data.get("eval_count", 0)

        return LLMResponse(
            content=text,
            provider="ollama",
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            estimated_cost=0.0,
            duration_seconds=round(time.time() - start, 2),
            raw=data,
        )

    async def stream(
        self,
        model: str,
        messages: list[dict],
        system: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        """Streaming via /api/generate. Mismo razonamiento que complete()."""
        import aiohttp

        prompt = self._build_prompt(messages, system)

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.host}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                resp.raise_for_status()
                async for line in resp.content:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        chunk = data.get("response", "")
                        if chunk:
                            yield chunk
                    except json.JSONDecodeError:
                        continue


# =============================================================================
# USAGE TRACKER
# =============================================================================

def _get_db_path() -> str:
    return os.getenv("VCORE_DB_PATH", "vcore.db")


def _log_usage(
    response: LLMResponse,
    agent_name: str = "",
    task_id: str = "",
    circuit_state: str = "CLOSED",
) -> None:
    """Registra cada llamada LLM en llm_usage_log."""
    try:
        conn = sqlite3.connect(_get_db_path())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            INSERT INTO llm_usage_log
            (provider, model, tokens_in, tokens_out, estimated_cost,
             timestamp, agent_name, task_id, circuit_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            response.provider,
            response.model,
            response.tokens_in,
            response.tokens_out,
            response.estimated_cost,
            time.time(),
            agent_name,
            task_id,
            circuit_state,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[UsageTracker] Warning: no se pudo loguear uso — {e}")


def _log_circuit_transition(
    provider: str,
    old_state: str,
    new_state: str,
    reason: str,
) -> None:
    """Registra transiciones del circuit breaker."""
    try:
        conn = sqlite3.connect(_get_db_path())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            INSERT INTO circuit_breaker_log
            (provider, transition, trigger_reason, error_rate, timestamp)
            VALUES (?, ?, ?, ?, ?)
        """, (
            provider,
            f"{old_state}->{new_state}",
            reason,
            0.0,
            time.time(),
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass  # silencioso — no romper el flujo por un log fallido


# =============================================================================
# OPENAI CLIENT (OpenAI-compatible, same API as DeepSeek)
# =============================================================================

class OpenAIClient(DeepSeekClient):
    """OpenAI via native API. Hereda de DeepSeekClient (misma API OpenAI-compatible)."""
    BASE_URL = "https://api.openai.com/v1"
    COST_PER_M_IN  = 2.50   # GPT-4o: $2.50/M input
    COST_PER_M_OUT = 10.00  # GPT-4o: $10.00/M output


# =============================================================================
# GITHUB MODELS CLIENT (OpenAI-compatible via Azure AI Inference)
# =============================================================================

class GitHubModelsClient(DeepSeekClient):
    """GitHub Models via Azure AI Inference API (OpenAI-compatible)."""
    BASE_URL = "https://models.inference.ai.azure.com"
    COST_PER_M_IN  = 0      # FREE durante el beta de GitHub Models
    COST_PER_M_OUT = 0


# =============================================================================
# NVIDIA NIM CLIENT (OpenAI-compatible via NVIDIA NIM API)
# =============================================================================

class NvidiaClient(DeepSeekClient):
    """NVIDIA NIM via OpenAI-compatible API (integrate.api.nvidia.com)."""
    BASE_URL = "https://integrate.api.nvidia.com/v1"
    COST_PER_M_IN  = 0      # NVIDIA NIM es gratis durante beta
    COST_PER_M_OUT = 0
    PROVIDER_NAME = "nvidia"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.BASE_URL,
                timeout=90.0,  # NIM models can be slow (DeepSeek Pro, Nemotron Ultra)
            )
        return self._client


# =============================================================================
# LLM ROUTER (factory config-driven)
# =============================================================================

class LLMRouter:
    """
    Factory + orquestador principal.
    Lee model_routing.yaml y gestiona circuit breakers, rate limiters y fallbacks.

    Uso:
        router = get_router()
        response = await router.complete(role="orchestrator_lead", messages=[...])
    """

    def __init__(self, config_path: str = "model_routing.yaml"):
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self._clients: dict[str, LLMClient] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._rate_limiters: dict[str, RateLimiter] = {}
        self._init_providers()

    def _load_config(self, path: str) -> dict:
        config_file = Path(path)
        if not config_file.exists():
            raise FileNotFoundError(
                f"model_routing.yaml no encontrado en {path}. "
                "Crealo antes de iniciar el router."
            )
        with open(config_file, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _init_providers(self) -> None:
        """Inicializa clientes, circuit breakers y rate limiters por provider."""
        cb_config = self.config.get("circuit_breaker", {})

        # Map model -> provider for _infer_provider
        self._model_to_provider = {
            # MAIN account (NVIDIA_KEY_MAIN)
            "z-ai/glm-5.2": "nvidia-lead",
            "z-ai/glm-5.1": "nvidia-lead",
            # FALLBACK account (NVIDIA_KEY_FALLBACK)
            "moonshotai/kimi-k2.6": "nvidia-kimi",
            "nvidia/nemotron-3-ultra-550b-a55b": "nvidia-nemotron",
            "nvidia/nemotron-mini-4b-instruct": "nvidia-lead",
            "nomic-embed-text": "ollama",
            "nvidia/llama-3.3-nemotron-super-49b-v1": "nvidia-nemotron",
            "deepseek-ai/deepseek-v4-flash": "nvidia-deepseek",
            "deepseek-ai/deepseek-v4-pro": "nvidia-deepseek",
            "mistralai/mistral-large-3-675b-instruct-2512": "nvidia-mistral-large",
            "meta/llama-3.2-90b-vision-instruct": "nvidia-mistral-large",
        }

        provider_map = {
            # Non-NVIDIA providers
            "gemini":     lambda: GeminiClient(api_key=os.getenv("GEMINI_API_KEY", "")),
            "deepseek":   lambda: DeepSeekClient(api_key=os.getenv("DEEPSEEK_API_KEY", "")),
            "anthropic":  lambda: AnthropicClient(api_key=os.getenv("ANTHROPIC_API_KEY", "")),
            "openai":     lambda: OpenAIClient(api_key=os.getenv("OPENAI_API_KEY", "")),
            "github":     lambda: GitHubModelsClient(api_key=os.getenv("GITHUB_TOKEN", "")),
            "ollama":     lambda: OllamaClient(host=os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")),
            # NVIDIA sub-providers (each with its own API key)
            "nvidia-lead":          lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_MAIN", "")),
            "nvidia-kimi":          lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_FALLBACK", "")),
            "nvidia-nemotron":      lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_FALLBACK", "")),
            "nvidia-deepseek":      lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_FALLBACK", "")),
            "nvidia-mistral-large": lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_AUX", "")),
            "nvidia-compress":      lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_COMPRESS", "")),
            "nvidia-aux-pool":      lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_AUX", "")),
            "nvidia-fallback":      lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_FALLBACK", "")),
            "nvidia-fallback-2":    lambda: NvidiaClient(api_key=os.getenv("NVIDIA_KEY_AUX", "")),
        }

        rpm_limits = {
            "gemini":    15,
            "deepseek":  60,
            "anthropic": 50,
            "openai":    500,
            "github":    50,
            "ollama":    999,
            # NVIDIA sub-providers each get their own RPM limit
            "nvidia-lead":          500,
            "nvidia-kimi":          500,
            "nvidia-nemotron":      500,
            "nvidia-deepseek":      500,
            "nvidia-mistral-large": 500,
            "nvidia-compress":      500,
            "nvidia-aux-pool":      500,
            "nvidia-fallback":      500,
            "nvidia-fallback-2":    500,
        }

        for provider, factory in provider_map.items():
            self._clients[provider] = factory()
            self._circuit_breakers[provider] = CircuitBreaker(
                provider=provider,
                error_threshold=cb_config.get("error_threshold", 0.30),
                window_seconds=cb_config.get("window_seconds", 60),
                recovery_timeout=cb_config.get("recovery_timeout", 120),
                min_calls_threshold=cb_config.get("min_calls_threshold", 5),
            )
            self._rate_limiters[provider] = RateLimiter(
                provider=provider,
                rpm_limit=rpm_limits.get(provider, 30),
            )

    def _get_role_config(self, role: str) -> dict:
        roles = self.config.get("roles", {})
        if role not in roles:
            raise ValueError(
                f"Rol '{role}' no definido en model_routing.yaml. "
                f"Roles disponibles: {list(roles.keys())}"
            )
        return roles[role]

    def _get_fallback_chain(self, provider: str) -> list[str]:
        return self.config.get("fallback_chains", {}).get(provider, [])

    async def complete(
        self,
        role: str,
        messages: list[dict],
        system: str = "",
        agent_name: str = "",
        task_id: str = "",
        override_max_tokens: Optional[int] = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        """
        Completion con circuit breaker, rate limiter y fallback chain.
        Loguea uso automaticamente.
        """
        role_config = self._get_role_config(role)
        provider    = role_config["provider"]
        model       = role_config["model"]
        # Resolve generic "nvidia" provider to specific sub-provider via model mapping
        if provider == "nvidia":
            provider = self._model_to_provider.get(model, "nvidia-lead")

        max_tokens  = override_max_tokens or role_config.get("max_tokens", 4096)
        temperature = role_config.get("temperature", 0.7)

        # ── Model preset: override temperature/tool strategy from model_presets ──
        model_preset = self.config.get("model_presets", {}).get(model, {})
        preset_tool_choice = None  # Let caller decide
        if model_preset:
            temperature = model_preset.get("temperature", temperature)
            preset_tool_choice = model_preset.get("tool_choice", None)
            # Only pass native tools if model supports it
            preset_strategy = model_preset.get("tool_strategy", "text_tool")
            if preset_strategy != "native_tc":
                tools = None  # Force text-based parsing for this model

        # ── Auto-context guard ──
        # Trim messages if they exceed the model's context window
        auto_cfg = self.config.get("auto_context", {})
        if auto_cfg.get("enabled", False) and model_preset:
            context_window = model_preset.get("context_window", 0)
            if context_window > 0:
                max_ratio = auto_cfg.get("max_context_ratio", 0.8)
                max_tokens_msg = int(context_window * max_ratio)
                # Rough estimate: 1 token ≈ 4 chars for English, ~2 chars for CJK
                estimated_tokens = sum(
                    len(m.get("content", "")) // 3 + 8  # +8 overhead per message
                    for m in messages
                ) + (len(system) // 3 if system else 0)
                if estimated_tokens > max_tokens_msg:
                    warn_ratio = auto_cfg.get("warn_threshold", 0.6)
                    if estimated_tokens > max_tokens_msg * warn_ratio:
                        print(f"[Router] Context guard: {estimated_tokens} est.tokens > {warn_ratio*100:.0f}% of {context_window} window for {model}")
                    # Trim oldest messages, keep last N
                    min_kept = auto_cfg.get("min_messages_kept", 4)
                    while len(messages) > min_kept and estimated_tokens > max_tokens_msg:
                        removed = messages.pop(0)
                        estimated_tokens -= (len(removed.get("content", "")) // 3 + 8)
                    print(f"[Router] Context guard: trimmed to {len(messages)} messages, ~{estimated_tokens} tokens for {model}")

        providers_to_try = [provider] + self._get_fallback_chain(provider)

        last_error = None
        for current_provider in providers_to_try:
            cb = self._circuit_breakers[current_provider]
            rl = self._rate_limiters[current_provider]

            if not cb.allow_request():
                print(f"[Router] {current_provider} OPEN — trying fallback")
                continue

            try:
                await rl.acquire()
                client = self._clients[current_provider]

                # Si es fallback, usar el mismo modelo base del provider
                current_model = model if current_provider == provider else \
                    self._get_default_model(current_provider)

                response = await client.complete(
                    model=current_model,
                    messages=messages,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    tools=tools,
                    tool_choice=tool_choice or preset_tool_choice,
                )

                cb.record_success()
                rl.record_success()
                _log_usage(response, agent_name, task_id, cb.state)
                return response

            except Exception as e:
                status_code = getattr(e, "status_code", 500)
                cb.record_error(status_code)
                if status_code == 429:
                    rl.record_429()
                last_error = e
                print(f"[Router] {current_provider} error {status_code}: {e}")
                continue

        raise RuntimeError(
            f"Todos los providers fallaron para rol '{role}'. "
            f"Ultimo error: {last_error}"
        )

    async def stream(
        self,
        role: str,
        messages: list[dict],
        system: str = "",
        agent_name: str = "",
        task_id: str = "",
    ) -> AsyncGenerator[str, None]:
        """Streaming con circuit breaker y rate limiter."""
        role_config = self._get_role_config(role)
        provider    = role_config["provider"]
        model       = role_config["model"]
        # Resolve generic "nvidia" provider to specific sub-provider via model mapping
        if provider == "nvidia":
            provider = self._model_to_provider.get(model, "nvidia-lead")

        max_tokens  = role_config.get("max_tokens", 4096)
        temperature = role_config.get("temperature", 0.7)

        cb = self._circuit_breakers[provider]
        rl = self._rate_limiters[provider]

        if not cb.allow_request():
            raise RuntimeError(f"Circuit breaker OPEN para {provider}")

        await rl.acquire()
        client = self._clients[provider]

        async for chunk in client.stream(
            model=model,
            messages=messages,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            yield chunk

        cb.record_success()
        rl.record_success()

    async def stream_complete(
        self,
        role: str,
        messages: list[dict],
        system: str = "",
        agent_name: str = "",
        task_id: str = "",
        override_max_tokens: Optional[int] = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
    ) -> AsyncGenerator["StreamChunk", None]:
        """
        Streaming real con circuit breaker, rate limiter, fallback chain,
        model presets y auto-context guard.

        Retorna StreamChunks tipados (text/tool_call/done).
        Si el provider falla, fallback al siguiente en la chain.
        Si el fallback no soporta stream_complete, usa complete() y emite todo como text chunk.
        """
        role_config = self._get_role_config(role)
        provider    = role_config["provider"]
        model       = role_config["model"]
        # Resolve generic "nvidia" provider to specific sub-provider via model mapping
        if provider == "nvidia":
            provider = self._model_to_provider.get(model, "nvidia-lead")

        max_tokens  = override_max_tokens or role_config.get("max_tokens", 4096)
        temperature = role_config.get("temperature", 0.7)

        # ── Model preset: override temperature/tool strategy from model_presets ──
        model_preset = self.config.get("model_presets", {}).get(model, {})
        preset_tool_choice = None
        if model_preset:
            temperature = model_preset.get("temperature", temperature)
            preset_tool_choice = model_preset.get("tool_choice", None)
            preset_strategy = model_preset.get("tool_strategy", "text_tool")
            if preset_strategy != "native_tc":
                tools = None  # Force text-based parsing for this model

        # ── Auto-context guard (reutiliza misma lógica que complete()) ──
        auto_cfg = self.config.get("auto_context", {})
        if auto_cfg.get("enabled", False) and model_preset:
            context_window = model_preset.get("context_window", 0)
            if context_window > 0:
                max_ratio = auto_cfg.get("max_context_ratio", 0.8)
                max_tokens_msg = int(context_window * max_ratio)
                estimated_tokens = sum(
                    len(m.get("content", "")) // 3 + 8
                    for m in messages
                ) + (len(system) // 3 if system else 0)
                if estimated_tokens > max_tokens_msg:
                    warn_ratio = auto_cfg.get("warn_threshold", 0.6)
                    if estimated_tokens > max_tokens_msg * warn_ratio:
                        print(f"[Router.stream] Context guard: {estimated_tokens} est.tokens > {warn_ratio*100:.0f}% of {context_window} window for {model}")
                    min_kept = auto_cfg.get("min_messages_kept", 4)
                    while len(messages) > min_kept and estimated_tokens > max_tokens_msg:
                        removed = messages.pop(0)
                        estimated_tokens -= (len(removed.get("content", "")) // 3 + 8)
                    print(f"[Router.stream] Context guard: trimmed to {len(messages)} messages, ~{estimated_tokens} tokens for {model}")

        providers_to_try = [provider] + self._get_fallback_chain(provider)
        last_error = None

        for current_provider in providers_to_try:
            cb = self._circuit_breakers[current_provider]
            rl = self._rate_limiters[current_provider]

            if not cb.allow_request():
                print(f"[Router.stream] {current_provider} OPEN — trying fallback")
                continue

            try:
                await rl.acquire()
                client = self._clients[current_provider]

                current_model = model if current_provider == provider else \
                    self._get_default_model(current_provider)

                # Intentar stream_complete del cliente
                stream_start = time.time()
                accumulated_content = ""
                accumulated_tokens_in = 0
                accumulated_tokens_out = 0
                final_tool_calls = None

                try:
                    async for chunk in client.stream_complete(
                        model=current_model,
                        messages=messages,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        tool_choice=tool_choice or preset_tool_choice,
                    ):
                        if chunk.type == "text":
                            accumulated_content += chunk.content
                            accumulated_tokens_out += 1  # rough estimate
                            yield chunk
                        elif chunk.type == "reasoning":
                            # Se reenvía tal cual: Orchestrator lo convierte en evento
                            # `reasoning` para el frontend, que lo muestra en un
                            # bloque colapsable. NO se suma a accumulated_content:
                            # el razonamiento no es la respuesta.
                            yield chunk
                        elif chunk.type == "tool_call":
                            final_tool_calls = chunk.tool_calls
                            accumulated_tokens_in = chunk.tokens_in or accumulated_tokens_in
                            yield chunk
                        elif chunk.type == "done":
                            accumulated_tokens_in = chunk.tokens_in or accumulated_tokens_in
                            yield chunk

                except AttributeError:
                    # Fallback: cliente no tiene stream_complete, usar complete() batch
                    response = await client.complete(
                        model=current_model,
                        messages=messages,
                        system=system,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        tools=tools,
                        tool_choice=tool_choice or preset_tool_choice,
                    )
                    accumulated_content = response.content
                    accumulated_tokens_in = response.tokens_in
                    accumulated_tokens_out = response.tokens_out
                    final_tool_calls = response.tool_calls

                    if response.content:
                        yield StreamChunk(type="text", content=response.content)
                    if response.tool_calls:
                        yield StreamChunk(type="tool_call", tool_calls=response.tool_calls)
                    yield StreamChunk(type="done", tokens_in=response.tokens_in)

                duration = round(time.time() - stream_start, 2)

                # Log usage (estimado para streaming — no hay usage exacto del stream)
                # Contar tokens de output por char estimate
                est_tokens_out = accumulated_tokens_out if accumulated_tokens_out > 1 else len(accumulated_content) // 3
                fake_response = LLMResponse(
                    content=accumulated_content,
                    provider=current_provider,
                    model=current_model,
                    tokens_in=accumulated_tokens_in,
                    tokens_out=est_tokens_out,
                    estimated_cost=0.0,
                    duration_seconds=duration,
                    raw=None,
                    tool_calls=final_tool_calls,
                )
                cb.record_success()
                rl.record_success()
                _log_usage(fake_response, agent_name, task_id, cb.state)
                return  # Éxito — no intentar más fallbacks

            except Exception as e:
                status_code = getattr(e, "status_code", 500)
                cb.record_error(status_code)
                if status_code == 429:
                    rl.record_429()
                last_error = e
                print(f"[Router.stream] {current_provider} error {status_code}: {e}")
                continue

        raise RuntimeError(
            f"Todos los providers fallaron para rol '{role}' (stream). "
            f"Ultimo error: {last_error}"
        )

    async def council(
        self,
        roles: list[str],
        messages: list[dict],
        system: str = "",
        agent_name: str = "",
        task_id: str = "",
    ) -> CouncilResult:
        """
        Dispara N roles en paralelo con el mismo prompt.
        Calcula divergencia por similitud de contenido.
        Si diverge 3 veces seguidas -> circuit breaker bloquea para revision humana.
        """
        tasks = [
            self.complete(
                role=role,
                messages=messages,
                system=system,
                agent_name=agent_name,
                task_id=task_id,
            )
            for role in roles
        ]

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        valid = [r for r in responses if isinstance(r, LLMResponse)]
        if not valid:
            raise RuntimeError("Council mode: todos los providers fallaron")

        divergence_score = self._calculate_divergence(valid)
        diverged = divergence_score > 0.3  # threshold configurable

        consensus = None
        if not diverged:
            # Tomar la respuesta mas larga como consenso (heuristica simple)
            consensus = max(valid, key=lambda r: len(r.content)).content

        # Registrar para circuit breaker
        for role in roles:
            role_config = self._get_role_config(role)
            provider = role_config["provider"]
            cb = self._circuit_breakers[provider]
            if diverged:
                cb.record_council_divergence()
            else:
                cb.record_council_consensus()

        return CouncilResult(
            responses=valid,
            diverged=diverged,
            divergence_score=divergence_score,
            consensus=consensus,
        )

    def _calculate_divergence(self, responses: list[LLMResponse]) -> float:
        """
        Divergencia simple basada en overlap de palabras entre respuestas.
        0.0 = identicas, 1.0 = completamente distintas.
        """
        if len(responses) < 2:
            return 0.0

        def word_set(text: str) -> set:
            return set(text.lower().split())

        sets = [word_set(r.content) for r in responses]
        intersection = sets[0]
        union = sets[0]
        for s in sets[1:]:
            intersection = intersection & s
            union = union | s

        if not union:
            return 0.0

        jaccard = len(intersection) / len(union)
        return round(1.0 - jaccard, 3)

    def _get_default_model(self, provider: str) -> str:
        defaults = {
            "nvidia":             "z-ai/glm-5.2",         # NIM default: GLM-5.2 (5.1 EOL 2026-07-02)
            "nvidia-lead":        "z-ai/glm-5.2",
            "nvidia-kimi":        "moonshotai/kimi-k2.6",
            "nvidia-nemotron":    "nvidia/nemotron-3-ultra-550b-a55b",
            "nvidia-deepseek":    "deepseek-ai/deepseek-v4-flash",
            "nvidia-mistral-large": "mistralai/mistral-large-3-675b-instruct-2512",
            "nvidia-fallback":    "z-ai/glm-5.2",
            "nvidia-fallback-2":  "z-ai/glm-5.2",
            "nvidia-compress":    "z-ai/glm-5.2",
            "nvidia-aux-pool":    "z-ai/glm-5.2",
            "gemini":             "gemini-3.5-flash",  # legacy, unreachable — Gemini provider no activo
            "deepseek":           "deepseek-v4-flash",
            "anthropic":          "claude-sonnet-4-6",
            "ollama":             "nomic-embed-text",   # solo embeddings, no chat
        }
        return defaults.get(provider, "unknown")

    def get_circuit_status(self) -> dict:
        """Retorna estado de todos los circuit breakers — para UsagePanel.jsx."""
        return {
            provider: {
                "state": cb.state,
                "consecutive_divergences": cb._consecutive_council_divergences,
            }
            for provider, cb in self._circuit_breakers.items()
        }

    # =========================================================================
    # HOT-SWAP: cambiar modelo en caliente sin reiniciar el server
    # =========================================================================

    def hot_swap(self, role: str, model: str, provider: str | None = None, temperature: float | None = None) -> dict:
        """
        Cambia el modelo de un rol en caliente.

        - Actualiza la config en memoria (self.config)
        - Resetea el circuit breaker del provider (por si estaba OPEN por 429/401)
        - Persiste el cambio en model_routing.yaml y VCORE_STATE.json
        - Retorna dict con status y el cambio realizado

        Uso:
            router.hot_swap("orchestrator_lead", "moonshotai/kimi-k2.6")
            router.hot_swap("orchestrator_lead", "deepseek-ai/deepseek-v4-flash", provider="nvidia")
        """
        roles = self.config.get("roles", {})
        if role not in roles:
            available = list(roles.keys())
            return {"ok": False, "error": f"Rol '{role}' no existe. Disponibles: {available}"}

        old_model = roles[role].get("model", "unknown")
        old_provider = roles[role].get("provider", "unknown")

        # Si no se especifica provider, inferirlo del modelo (NIM models = nvidia)
        if provider is None:
            provider = self._infer_provider(model, old_provider)

        # Actualizar config en memoria
        roles[role]["model"] = model
        if provider != old_provider:
            roles[role]["provider"] = provider
        if temperature is not None:
            roles[role]["temperature"] = temperature
        self.config["roles"] = roles

        # Resetear circuit breaker del nuevo provider (por si estaba OPEN)
        if provider in self._circuit_breakers:
            cb = self._circuit_breakers[provider]
            cb.state = "CLOSED"
            cb._error_times.clear()
            print(f"[HotSwap] Circuit breaker '{provider}' reseteado a CLOSED")

        # Persistir en model_routing.yaml
        self._persist_yaml()

        # Persistir en VCORE_STATE.json
        self._persist_state(role, model)

        print(f"[HotSwap] {role}: {old_model} ({old_provider}) → {model} ({provider})")

        return {
            "ok": True,
            "role": role,
            "old_model": old_model,
            "new_model": model,
            "old_provider": old_provider,
            "new_provider": provider,
            "circuit_breaker_reset": True,
        }

    def _infer_provider(self, model: str, fallback: str) -> str:
        """Infiere el provider a partir del nombre del modelo."""
        # Usar mapa explícito modelo -> provider (configurado en _init_providers)
        if model in self._model_to_provider:
            return self._model_to_provider[model]
        # Fallback a prefijos genéricos
        nvidia_prefixes = [
            "deepseek-ai/", "meta/", "nvidia/", "mistralai/", "moonshotai/",
            "z-ai/", "qwen/", "minimaxai/", "stepfun-ai/", "openai/",
            "google/", "microsoft/", "ibm/", "bytedance/",
        ]
        for prefix in nvidia_prefixes:
            if model.startswith(prefix):
                return "nvidia"
        return fallback

    def _persist_yaml(self) -> None:
        """Persiste la config actual en model_routing.yaml y en la sesión default.

        El 3-point config sync exige que el global y sessions/default/
        concuerden. Antes solo se escribía el global: al reiniciar,
        _ensure_default_session() veía que diferían y copiaba el default
        viejo ENCIMA, así que todo hot-swap se perdía silenciosamente.
        """
        try:
            import yaml
            yaml_path = Path(self.config_path) if hasattr(self, 'config_path') and self.config_path else Path("model_routing.yaml")
            with open(yaml_path, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

            # Sincronizar la sesión default (fuente de verdad del 3-point sync).
            default_yaml = Path(__file__).resolve().parent.parent / "sessions" / "default" / "model_routing.yaml"
            if default_yaml.exists() and default_yaml.resolve() != yaml_path.resolve():
                with open(default_yaml, "w", encoding="utf-8") as f:
                    yaml.dump(self.config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        except Exception as e:
            print(f"[HotSwap] Error persistiendo YAML: {e}")

    def _persist_state(self, role: str, model: str) -> None:
        """Persiste el cambio en VCORE_STATE.json."""
        try:
            state_path = Path("VCORE_STATE.json")
            if not state_path.exists():
                return
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            # Mapear rol → campo en STATE
            role_to_state_key = {
                "orchestrator_lead": "orchestrator_lead_model",
                "orchestrator_council": "orchestrator_council_model",
                "orchestrator_escalation": "orchestrator_escalation_model",
                "planner_plan": "planner_plan_model",
                "planner_apply": "planner_apply_model",
                "planner_escalation": "planner_escalation_model",
                "curator": "curator_model",
                "retriever": "retriever_model",
            }
            key = role_to_state_key.get(role)
            if key:
                state[key] = model
                state["fecha_actualizacion"] = datetime.now(timezone.utc).isoformat()
                with open(state_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"[HotSwap] Error persistiendo STATE: {e}")

    def get_active_models(self) -> dict:
        """Retorna los modelos activos de cada rol — para el endpoint GET /system/model."""
        roles = self.config.get("roles", {})
        presets = self.config.get("model_presets", {})
        result = {}
        for role_name, role_config in roles.items():
            provider = role_config.get("provider", "?")
            model = role_config.get("model", "?")
            cb_state = self._circuit_breakers.get(provider, CircuitBreaker(provider="?")).state
            # Add context window info from preset
            preset = presets.get(model, {})
            result[role_name] = {
                "provider": provider,
                "model": model,
                "circuit_breaker": cb_state,
                "context_window": preset.get("context_window", 0),
                "max_output": preset.get("max_output", 0),
                "tool_strategy": preset.get("tool_strategy", "text_tool"),
                "tool_choice": preset.get("tool_choice", "auto"),
                "max_tool_calls": preset.get("max_tool_calls", 3),
                # El role_config es la autoridad de la temperatura (es lo que
                # lee cada llamada al LLM); el preset es solo el fallback. Sin
                # exponerlo, el selector de temperatura de la UI no podia
                # reflejar el estado real.
                "temperature": role_config.get("temperature", preset.get("temperature", 0.7)),
            }
        return result

    def get_available_models(self, provider: str = "nvidia") -> list[dict]:
        """Lista modelos disponibles con context window info — para UI de selection."""
        # Read from model_routing.yaml available_models (authoritative)
        available = self.config.get("available_models", [])
        if available and provider == "nvidia":
            return available
        # Fallback: hard-coded list with basic info
        nvidia_models = [
            {"id": "moonshotai/kimi-k2.6", "name": "Kimi K2.6", "context": "256K"},
            {"id": "z-ai/glm-5.1", "name": "GLM 5.1", "context": "200K"},
            {"id": "deepseek-ai/deepseek-v4-flash", "name": "DeepSeek V4 Flash", "context": "1M"},
            {"id": "deepseek-ai/deepseek-v4-pro", "name": "DeepSeek V4 Pro", "context": "1M"},
            {"id": "nvidia/nemotron-3-ultra-550b-a55b", "name": "Nemotron 3 Ultra 550b", "context": "1M"},
        ]
        if provider == "nvidia":
            return nvidia_models
        return []


# =============================================================================
# SINGLETON
# =============================================================================

_router_instance: Optional[LLMRouter] = None


def get_router(config_path: str = "model_routing.yaml", force_new: bool = False) -> LLMRouter:
    """Retorna el router singleton. Inicializa en el primer llamado.
    
    Si force_new=True, crea una nueva instancia sin afectar el singleton.
    Útil para session isolation — cada sesión tiene su propio router.
    """
    global _router_instance
    if force_new:
        return LLMRouter(config_path=config_path)
    if _router_instance is None:
        _router_instance = LLMRouter(config_path=config_path)
    return _router_instance


def set_router(router: LLMRouter) -> None:
    """Reemplaza el singleton — para session isolation temporal."""
    global _router_instance
    _router_instance = router


def reset_router() -> None:
    """Reset del singleton — util para tests."""
    global _router_instance
    _router_instance = None


def reset_circuit_breakers() -> None:
    """Resetea todos los circuit breakers a CLOSED — util tras fallos transitorios."""
    global _router_instance
    if _router_instance is not None:
        for provider, cb in _router_instance._circuit_breakers.items():
            cb.state = "CLOSED"
            cb._consecutive_council_divergences = 0
            cb._error_times.clear()
            cb._total_calls = 0
            cb._recovery_at = 0.0
            print(f"[CircuitBreaker] {provider}: -> CLOSED (manual reset)")


# =============================================================================
# DEBUG / CLI
# =============================================================================

if __name__ == "__main__":
    async def _smoke_test():
        print("=== V-CORE LLM Router — smoke test ===")
        router = get_router()

        print("\n[1] Circuit breaker status:")
        for provider, status in router.get_circuit_status().items():
            print(f"  {provider}: {status['state']}")

        print("\n[2] Roles configurados:")
        for role in router.config.get("roles", {}):
            cfg = router._get_role_config(role)
            print(f"  {role}: {cfg['provider']} / {cfg['model']}")

        print("\n[3] Test completion (orchestrator_lead -> Gemini):")
        try:
            response = await router.complete(
                role="orchestrator_lead",
                messages=[{"role": "user", "content": "Di 'hola' en una palabra."}],
                agent_name="smoke_test",
            )
            print(f"  OK: '{response.content.strip()}'")
            print(f"  tokens: in={response.tokens_in} out={response.tokens_out}")
            print(f"  duration: {response.duration_seconds}s")
        except Exception as e:
            print(f"  ERROR: {e}")

        print("\n=== smoke test completo ===")

    asyncio.run(_smoke_test())
