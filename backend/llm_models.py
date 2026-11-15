"""Model capabilities verified against OpenAI's model documentation."""

from dataclasses import dataclass, asdict
from typing import Optional

DEFAULT_MODEL = "gpt-6-luna"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class LLMModelInfo:
    name: str
    api_string: str
    price_input_per_1m: float
    price_output_per_1m: float
    price_cached_per_1m: float
    reasoning_efforts: tuple[str, ...]
    long_context: bool = False


MODEL_REGISTRY = {}


def register(id, label, inp, out, cached, efforts, long=False):
    MODEL_REGISTRY[id] = LLMModelInfo(label, id, inp, out, cached, efforts, long)


for id, label, inp, out, cached, none in [
    ("gpt-6-luna", "GPT-6 Luna", 0.1, 0.5, 0.01, True),
    ("gpt-6.1-sol", "GPT-6.1 Sol", 2, 10, 0.1, False),
    ("gpt-6.1-terra", "GPT-6.1 Terra", 1, 5, 0.05, True),
    ("gpt-6-astra", "GPT-6 Astra", 10, 50, 1, False),
]:
    register(
        id,
        label,
        inp,
        out,
        cached,
        (("none",) if none else ()) + ("low", "medium", "high", "xhigh", "max"),
        True,
    )
register(
    "gpt-5-mini", "GPT-5 Mini", 0.25, 2, 0.025, ("minimal", "low", "medium", "high")
)
register("gpt-5", "GPT-5", 1.25, 10, 0.125, ("minimal", "low", "medium", "high"))
register("gpt-5.1", "GPT-5.1", 1.25, 10, 0.125, ("none", "low", "medium", "high"))
register("gpt-5.1-codex", "GPT-5.1 Codex", 1.25, 10, 0.125, ("low", "medium", "high"))


def get_model_info(name: str) -> Optional[LLMModelInfo]:
    return MODEL_REGISTRY.get(name.strip())


def get_api_string(name: str) -> str:
    return name.strip()


def get_available_models():
    return MODEL_REGISTRY.copy()


def is_valid_model(name):
    return get_model_info(name) is not None


def validate_settings(model, effort=None):
    model = model.strip()
    if not model or any(c.isspace() for c in model) or len(model) > 200:
        raise ValueError("Provide a valid model ID without whitespace.")
    effort = (effort or "").strip().lower()
    if effort in ("", "default"):
        return model, None
    info = get_model_info(model)
    allowed = info.reasoning_efforts if info else EFFORTS
    if effort not in allowed:
        raise ValueError(
            f"Reasoning effort for {model} must be one of: {', '.join(allowed)}"
        )
    return model, effort


def estimate_response(model, response):
    info = get_model_info(model)
    usage = getattr(response, "usage", None)
    tier = getattr(response, "service_tier", None) or "default"
    if not info or not usage or tier not in ("default", "flex"):
        return None
    inp, out = usage.input_tokens, usage.output_tokens
    details = getattr(usage, "input_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    # Unknown cache-write accounting must not produce a misleading estimate.
    if getattr(details, "cache_write_tokens", 0):
        return None
    long = info.long_context and inp > 272000
    cost = (
        (inp - cached) * info.price_input_per_1m + cached * info.price_cached_per_1m
    ) * (2 if long else 1)
    cost += out * info.price_output_per_1m * (1.5 if long else 1)
    return cost / 1_000_000 * (0.5 if tier == "flex" else 1)
