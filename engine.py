import os
import math
import logging
import outlines
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Optional
import httpx

# Google SDK imports
from google import genai
from google.genai import types

# Local Llama imports
from llama_cpp import Llama
from outlines.templates import Template
from huggingface_hub import hf_hub_download
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config.settings import settings
from schemas import ScheduleCalendarEventFunction
from utilities.circuit_breaker import is_tripped, trip

import httpx

try:
    from google.api_core import exceptions as google_exceptions
except Exception:
    google_exceptions = None

logger = logging.getLogger(__name__)


# Static System Instructions (Exact string match across all calls for Maximum Cache Hits)
system_instruction = str(Template.from_file("utilities/templates/system_prompt.txt"))


# ==========================================
# 1. BASE PROVIDER INTERFACE
# ==========================================
class BaseLLMProvider(ABC):
    @abstractmethod
    def generate_schedule(
        self, system_instruction: str, user_content: str
    ) -> ScheduleCalendarEventFunction:
        pass


# ==========================================
# 2. OPENROUTER PROVIDER
# ==========================================
class OpenRouterProvider(BaseLLMProvider):
    def __init__(self, model_name: str = "google/gemini-2.5-flash-lite"):
        self.api_key = settings.llm.open_router_api_key.get_secret_value()
        self.model_name = model_name

    def generate_schedule(
        self, system_instruction: str, user_content: str
    ) -> ScheduleCalendarEventFunction:
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "Calendar Task Engine",
        }

        # Enforce JSON Schema via OpenRouter structured output
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": system_instruction,
                            "cache_control": {
                                "type": "ephemeral"
                            },  # Explicit breakpoint
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": user_content,
                        }
                    ],
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ScheduleCalendarEventFunction",
                    "strict": True,
                    "schema": ScheduleCalendarEventFunction.model_json_schema(),
                },
            },
        }

        with httpx.Client(timeout=15.0) as client:
            response = client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            result = response.json()

            logger.info(result)

            # return raw token metrics from response objects
            # without doing cost calculations inside
            usage = result.get("usage", {})

            logger.info(usage)

            meta = {
                "provider": "openrouter",
                "model_name": self.model_name,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "cached_tokens": usage.get("prompt_tokens_details", {}).get(
                    "cached_tokens", 0
                ),
                "raw_meta": {"id": result.get("id")},
            }
            raw_content = result["choices"][0]["message"]["content"]

            return ScheduleCalendarEventFunction.model_validate_json(raw_content), meta


# ==========================================
# 3. GOOGLE PROVIDER
# ==========================================
class GoogleProvider(BaseLLMProvider):
    def __init__(
        self, model_name: str = "gemini-2.5-flash-lite", ttl_seconds: int = 300
    ):

        self.api_key = settings.llm.gemini_api_key.get_secret_value()
        self.model_name = model_name
        self.ttl_seconds = ttl_seconds
        self.client = self._init_client()
        self.cached_content = None

    def _init_client(self) -> genai.Client | None:
        if self.api_key:
            return genai.Client(api_key=self.api_key)
        return None

    def _get_or_create_cached_content(self, system_instruction: str):
        """Creates or reuses explicit Gemini Cache with configurable TTL."""
        if self.cached_content is not None:
            try:
                # Return existing active cache if valid
                return self.cached_content.name
            except Exception:
                self.cached_content = None

        logger.info(
            f"Creating explicit Gemini cached content with TTL={self.ttl_seconds}s..."
        )
        # Create explicit cache via Google GenAI SDK
        self.cached_content = self.client.caches.create(
            model=self.model_name,
            config=types.CreateCachedContentConfig(
                contents=[system_instruction],
                ttl=f"{self.ttl_seconds}s",
                display_name="calendar_engine_system_instructions",
            ),
        )
        return self.cached_content.name

    def generate_schedule(
        self, system_instruction: str, user_content: str
    ) -> ScheduleCalendarEventFunction:

        if not self.api_key or not self.client:
            raise ValueError("GEMINI_API_KEY is missing or invalid.")

        cache_name = self._get_or_create_cached_content(system_instruction)

        # Native schema enforcement via Google GenAI SDK
        # Pass static instructions into system_instruction field for API prompt caching
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=user_content,
            config=types.GenerateContentConfig(
                cached_content=cache_name,
                response_mime_type="application/json",
                response_schema=ScheduleCalendarEventFunction,
                thinking_config=types.ThinkingConfig(
                    thinking_level="low"  # Restricts deep reasoning tokens to reduce latency
                ),
            ),
        )

        if not response.text:
            raise RuntimeError("Gemini API returned an empty response.")

        # Extract token metadata from Google GenAI SDK response object
        usage = getattr(response, "usage_metadata", None)
        meta = {
            "provider": "google",
            "model_name": self.model_name,
            "prompt_tokens": getattr(usage, "prompt_token_count", 0) if usage else 0,
            "completion_tokens": (
                getattr(usage, "candidates_token_count", 0) if usage else 0
            ),
            "cached_tokens": (
                getattr(usage, "cached_content_token_count", 0) if usage else 0
            ),
            "raw_meta": {
                "response_id": getattr(response, "response_id", None),
                "model_version": getattr(response, "model_version", None),
            },
        }

        return ScheduleCalendarEventFunction.model_validate_json(response.text), meta


# ==========================================
# 4. LOCAL LLAMA PROVIDER (LAZY LOADED)
# ==========================================
class LocalLlamaProvider(BaseLLMProvider):
    def __init__(
        self,
        repo_id: str = "bartowski/Qwen2.5-0.5B-Instruct-GGUF",
        filename: str = "Qwen2.5-0.5B-Instruct-f16.gguf",
        cache_dir: str = "./models/huggingface",
    ):
        self.repo_id = repo_id
        self.filename = filename
        self.cache_dir = cache_dir
        self._model = None  # Lazy-load holder

    def _initialize_model(self):
        """Loads weights only when first invoked."""

        if self._model is None:
            logger.info(f"Initializing local GGUF model: {self.filename}")
            n_threads = max(3, math.ceil((os.cpu_count() or 4) / 2))

            model_path = hf_hub_download(
                repo_id=self.repo_id,
                filename=self.filename,
                cache_dir=self.cache_dir,
                token=settings.llm.hf__token.get_secret_value(),
            )

            llm = Llama(model_path=model_path, n_ctx=2048, n_threads=n_threads)
            self._model = outlines.from_llamacpp(llm)

    def generate_schedule(
        self, system_instruction: str, user_content: str
    ) -> ScheduleCalendarEventFunction:

        self._initialize_model()

        formatted_prompt = (
            f"<|im_start|>system\n{system_instruction}<|im_end|>\n"
            f"<|im_start|>user\n{user_content}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )

        raw_json = self._model(
            formatted_prompt,
            output_type=ScheduleCalendarEventFunction,
            temperature=0.0,
            max_tokens=300,
            stop=["<|im_end|>", "<|endoftext|>"],
        )

        # Local CPU/GGUF models cost $0 USD and don't report remote API usage
        meta = {
            "provider": "local",
            "model_name": self.filename,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "raw_meta": {"repo_id": self.repo_id, "filename": self.filename},
        }

        return ScheduleCalendarEventFunction.model_validate_json(raw_json), meta


# ==========================================
# 5. FALLBACK ENGINE ORCHESTRATOR
# ==========================================
class CalendarFunctionEngine:
    def __init__(self, default_tz: str = "UTC"):
        # Define provider priority sequence
        self.providers = [OpenRouterProvider(), GoogleProvider(), LocalLlamaProvider()]
        # Default timezone used when invalid tz strings are encountered
        self.default_tz = default_tz

    # This protects against off-by-one-hour scheduling errors during DST transitions
    def _parse_and_format_timezone_context(
        self, user_tz_str: str, anchor_dt: Optional[datetime] = None
    ) -> Dict[str, str]:
        """Resolve IANA timezone and emit wall-clock context for deterministic scheduling."""
        if not anchor_dt:
            anchor_dt = datetime.now(timezone.utc)

        try:
            tz = ZoneInfo(user_tz_str)
        except (ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo(self.default_tz)

        localized_dt = anchor_dt.astimezone(tz)
        utc_offset = localized_dt.utcoffset()
        total_seconds = int(utc_offset.total_seconds()) if utc_offset else 0
        hours = total_seconds // 3600
        minutes = abs(total_seconds % 3600) // 60
        offset_formatted = f"{hours:+03d}:{minutes:02d}"
        is_dst = bool(localized_dt.dst() and localized_dt.dst().total_seconds() != 0)

        return {
            "iana_identifier": tz.key,
            "iso_now": localized_dt.isoformat(),
            "utc_offset": offset_formatted,
            "is_dst": str(is_dst),
            "tz_abbreviation": localized_dt.tzname() or "UTC",
        }

    def _build_prompt(self, request_text: str) -> str:
        now_utc = datetime.now(timezone.utc)
        current_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        current_date_str = now_utc.strftime("%Y-%m-%d (%A)")
        timezone_context = self._parse_and_format_timezone_context(
            self.default_tz, now_utc
        )

        return (
            f"CURRENT DATETIME REFERENCE (UTC): {current_iso}\n"
            f"CURRENT DATE: {current_date_str}\n"
            f"TIMEZONE CONTEXT: iana={timezone_context['iana_identifier']}, "
            f"local_iso_now={timezone_context['iso_now']}, "
            f"utc_offset={timezone_context['utc_offset']}, "
            f"is_dst={timezone_context['is_dst']}, "
            f"tz_abbreviation={timezone_context['tz_abbreviation']}\n\n"
            f"User Request: {request_text}"
        )

    def extract_calendar_function(
        self, request_text: str
    ) -> ScheduleCalendarEventFunction:

        # Change _build_user_content to _build_prompt
        user_content = self._build_prompt(request_text)

        for provider in self.providers:
            provider_name = provider.__class__.__name__

            # Skip provider if circuit is currently tripped
            if is_tripped(provider_name):
                logger.warning(
                    "Skipping provider %s because circuit is tripped", provider_name
                )
                continue

            try:
                logger.info(f"Attempting extraction via {provider_name}...")
                return provider.generate_schedule(system_instruction, user_content)
            except Exception as e:
                # Provider-specific precise checks
                tripped = False

                # httpx HTTPStatusError (OpenRouter) -> check for 429
                if isinstance(e, httpx.HTTPStatusError):
                    status_code = getattr(e.response, "status_code", None)
                    if status_code == 429:
                        tripped = True

                # Google SDK quota/too many requests
                if google_exceptions is not None and isinstance(
                    e,
                    (
                        getattr(google_exceptions, "TooManyRequests", type(None)),
                        getattr(google_exceptions, "ResourceExhausted", type(None)),
                    ),
                ):
                    tripped = True

                if tripped:
                    try:
                        trip(provider_name, ttl_seconds=60)
                    except Exception:
                        pass

                logger.warning(f"[{provider_name} Failed]: {e}. Attempting fallback...")

        raise RuntimeError("All LLM providers in the fallback chain failed.")


# Singleton accessor
_engine_instance = None


def get_calendar_engine() -> CalendarFunctionEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = CalendarFunctionEngine()
    return _engine_instance
