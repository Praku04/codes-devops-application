"""LiteLLM client for AWS Bedrock with retries, timeout, model fallback, and JSON repair."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import litellm

from app.core.config import get_settings
from app.core.exceptions import AppError

logger = logging.getLogger("agentcraft.llm")

# The same two ids as `Settings.llm_model` / `llm_fallback_model`, named once here and used
# everywhere below. They were previously written out at each of the four use sites, which is
# how a model swap turns into a hunt: three of them agreeing and one left behind reads as a
# working config right up until the primary is unavailable.
#
# The two ids are shaped differently on purpose, and neither is a typo — see the notes on
# `_PROFILE_ONLY_MODELS` (why 4.6 needs the `us.` prefix) and `_NO_SAMPLING_MODELS` (why
# Sonnet 5 must not be sent a temperature) below.
DEFAULT_PRIMARY_MODEL = "bedrock/us.anthropic.claude-sonnet-4-6"
DEFAULT_FALLBACK_MODEL = "bedrock/anthropic.claude-sonnet-5"


def _sync_aws_env() -> None:
    settings = get_settings()
    if settings.aws_access_key_id:
        os.environ["AWS_ACCESS_KEY_ID"] = settings.aws_access_key_id
    if settings.aws_secret_access_key:
        os.environ["AWS_SECRET_ACCESS_KEY"] = settings.aws_secret_access_key
    if settings.aws_session_token:
        os.environ["AWS_SESSION_TOKEN"] = settings.aws_session_token
    os.environ["AWS_REGION_NAME"] = settings.aws_region_name
    os.environ["AWS_DEFAULT_REGION"] = settings.aws_region_name
    os.environ["AWS_REGION"] = settings.aws_region_name


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_truncated_json(text: str) -> str:
    """Best-effort repair when the model hits max_tokens mid-JSON."""
    text = _strip_fences(text)
    # Keep from first { 
    start = text.find("{")
    if start < 0:
        return text
    text = text[start:]

    in_string = False
    escape = False
    stack: list[str] = []
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    # Close open string
    if in_string:
        text += '"'
    # Drop trailing comma / colon junk
    text = re.sub(r",\s*$", "", text.rstrip())
    text = re.sub(r":\s*$", ': ""', text.rstrip())
    # Close open structures
    while stack:
        text += stack.pop()
    return text


def _extract_json(text: str) -> Any:
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*", cleaned)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            repaired = _repair_truncated_json(candidate)
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                # Try cutting back to last complete object-ish closing brace
                for i in range(len(repaired) - 1, 0, -1):
                    if repaired[i] == "}":
                        try:
                            return json.loads(repaired[: i + 1])
                        except json.JSONDecodeError:
                            continue
                raise
    raise json.JSONDecodeError("No JSON object found", cleaned, 0)


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "expired",
            "security token",
            "expiredtoken",
            "invalidsecuritytoken",
            "unrecognizedclient",
            "invalidclienttokenid",
            "the security token included in the request is invalid",
            "unable to locate credentials",
            "could not find credentials",
            "no credentials",
        )
    )


def _is_retryable(exc: Exception) -> bool:
    if _is_auth_error(exc):
        return False
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    return any(
        token in name or token in msg
        for token in (
            "timeout",
            "timed out",
            "rate",
            "429",
            "503",
            "throttl",
            "unavailable",
            "connection",
            "accessdenied",
            "resourcenotfound",
            "validation",
        )
    )


_AUTH_HELP = (
    "AWS Bedrock credentials expired or invalid. "
    "Update AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN in .env, "
    "then restart the API."
)


# Bedrock offers the models used here through cross-region inference rather than on-demand
# throughput, so passing a *base* model id fails outright:
#
#   Invocation of model ID anthropic.claude-sonnet-4-6 with on-demand throughput isn't
#   supported. Retry your request with the ID or ARN of an inference profile that contains
#   this model.
#
# An inference profile id is the base id with a routing prefix in front of it — `global.` for
# dynamic routing at no premium, or `us.`/`eu.`/`jp.`/`apac.` to guarantee the region (10%
# premium). Catching a prefix-less id here costs one warning at startup of the call instead of
# a 400 in the middle of a generation the user is waiting on.
#
# This is a list of *specific* models rather than a rule about bare `anthropic.…` ids, because
# there is no such rule: the newest models are the other way round. Sonnet 5 (and Opus 5, Opus
# 4.7/4.8, Fable 5/5.1) have no ARN-versioned model id and no inference profile — they are
# reached through InvokeModel with the bare `anthropic.claude-sonnet-5`, and prefixing *that*
# is the error. So the fallback id below is deliberately unprefixed, and must not be "fixed".
# Point this at some third model and the worst case is Bedrock's own 400, which states the fix.
_ROUTING_PREFIXES = ("global.", "us.", "eu.", "jp.", "apac.")
_PROFILE_ONLY_MODELS = (
    "anthropic.claude-sonnet-4-6",
    "anthropic.claude-haiku-4-5",
)


def _is_unsupported_on_demand(model: str) -> bool:
    bare = model.removeprefix("bedrock/")
    if bare.startswith(_ROUTING_PREFIXES):
        return False
    return bare.startswith(_PROFILE_ONLY_MODELS)


# From Sonnet 5 on, sampling parameters are gone: `temperature`, `top_p` and `top_k` are 400s,
# not warnings, at any non-default value. Those models manage their own sampling.
#
# So this is per-model rather than dropping temperature everywhere — 0.2 is right for Sonnet 4.6
# and Haiku 4.5, which still accept it, and only the models below must not see it at all.
#
# It matters most for the *fallback*, which is where it would go unnoticed: the fallback only
# runs when the primary is already failing, so a 400 here turns "Sonnet was throttled, Sonnet 5
# answered" into a failed generation, reported with the second model's confusing error rather
# than the first's. Worse, "validation" counts as retryable in `_is_retryable`, so it would burn
# every attempt on an error that cannot succeed.
_NO_SAMPLING_MODELS = (
    "anthropic.claude-sonnet-5",
    "anthropic.claude-opus-5",
    "anthropic.claude-opus-4-7",
    "anthropic.claude-opus-4-8",
    "anthropic.claude-fable-5",
)


def _accepts_sampling_params(model: str) -> bool:
    """False when `temperature` must be omitted for this model rather than merely left alone."""
    bare = model.removeprefix("bedrock/")
    for prefix in _ROUTING_PREFIXES:
        bare = bare.removeprefix(prefix)
    return not bare.startswith(_NO_SAMPLING_MODELS)


def _model_chain() -> list[str]:
    settings = get_settings()
    primary = (settings.llm_model or DEFAULT_PRIMARY_MODEL).strip()
    fallback = (settings.llm_fallback_model or DEFAULT_FALLBACK_MODEL).strip()
    chain: list[str] = []
    for candidate in (primary, fallback, DEFAULT_PRIMARY_MODEL, DEFAULT_FALLBACK_MODEL):
        if not candidate or candidate in chain:
            continue
        if _is_unsupported_on_demand(candidate):
            logger.warning("Skipping unsupported on-demand Bedrock model id: %s", candidate)
            continue
        chain.append(candidate)
    return chain or [DEFAULT_PRIMARY_MODEL]


class LLMClient:
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        timeout: float | None = None,
    ) -> str:
        settings = get_settings()
        _sync_aws_env()
        read_timeout = float(timeout) if timeout is not None else float(max(settings.llm_timeout_seconds, 300))
        token_cap = max_tokens if max_tokens is not None else min(settings.llm_max_tokens, 8192)
        # Everything except `temperature`, which is added per model below: the chain can mix a
        # model that accepts it with one that rejects it outright, and this dict is shared by
        # every model in it.
        base_kwargs: dict[str, Any] = {
            "messages": messages,
            "max_tokens": token_cap,
            "timeout": read_timeout,
            "num_retries": 0,
        }
        if json_mode:
            base_kwargs["response_format"] = {"type": "json_object"}
        wanted_temperature = temperature if temperature is not None else settings.llm_temperature

        last_exc: Exception | None = None
        for model in _model_chain():
            kwargs = {**base_kwargs, "model": model}
            if _accepts_sampling_params(model):
                kwargs["temperature"] = wanted_temperature
            else:
                # Omitted, not clamped: this model picks its own sampling, and sending the value
                # at all is the 400. Callers asking for 0.0 to get determinism do not get it here.
                logger.debug("Omitting temperature=%s for %s", wanted_temperature, model)
            for attempt in range(retries + 1):
                try:
                    response = await litellm.acompletion(**kwargs)
                    content = response.choices[0].message.content or ""
                    usage = getattr(response, "usage", None)
                    finish = None
                    try:
                        finish = response.choices[0].finish_reason
                    except Exception:
                        pass
                    logger.info(
                        "llm_ok model=%s chars=%s attempt=%s finish=%s usage=%s",
                        model,
                        len(content),
                        attempt + 1,
                        finish,
                        usage,
                    )
                    return content
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "LiteLLM failed model=%s attempt=%s/%s: %s",
                        model,
                        attempt + 1,
                        retries + 1,
                        exc,
                    )
                    if _is_auth_error(exc):
                        raise AppError(_AUTH_HELP, status_code=502) from exc
                    if attempt < retries and _is_retryable(exc):
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    break

        logger.exception("LiteLLM completion failed all models")
        if last_exc and _is_auth_error(last_exc):
            raise AppError(_AUTH_HELP, status_code=502) from last_exc
        raise AppError(
            f"LLM request failed after retries/fallback: {last_exc}. "
            "Verify AWS credentials/session token, region, Bedrock model access, and network.",
            status_code=502,
        ) from last_exc

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        timeout: float | None = None,
    ) -> Any:
        text = await self.complete(
            messages,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
            timeout=timeout,
        )
        try:
            return _extract_json(text)
        except Exception as first_exc:
            logger.warning("JSON parse failed (%s); retrying compact regeneration", first_exc)
            # Retry once: ask for a smaller, complete JSON object with more room
            retry_messages = list(messages) + [
                {"role": "assistant", "content": text[:6000]},
                {
                    "role": "user",
                    "content": (
                        "Your previous JSON was truncated or invalid. "
                        "Reply with ONE complete valid JSON object only. "
                        "Keep every string short (<=120 chars). Do not truncate."
                    ),
                },
            ]
            retry_cap = max(max_tokens or 4000, 6000)
            text2 = await self.complete(
                retry_messages,
                json_mode=True,
                temperature=0.1,
                max_tokens=retry_cap,
                retries=retries,
                timeout=timeout,
            )
            try:
                return _extract_json(text2)
            except Exception as exc:
                raise AppError(f"Failed to parse LLM JSON: {exc}", status_code=502) from exc


llm_client = LLMClient()
