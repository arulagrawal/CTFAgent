"""Shared bounded structured Responses requests."""

import asyncio
from .llm_models import validate_settings


class ModelResponseError(RuntimeError):
    pass


async def structured_response(
    client,
    *,
    model,
    schema,
    instructions,
    input,
    effort=None,
    tier="default",
    previous_id=None,
    timeout=120,
):
    model, effort = validate_settings(model, effort)
    if tier not in ("default", "flex"):
        raise ValueError("OPENAI_SERVICE_TIER must be default or flex")
    kwargs = dict(
        model=model,
        instructions=instructions,
        input=input,
        text_format=schema,
        max_output_tokens=32768,
        service_tier=tier,
    )
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if previous_id:
        kwargs["previous_response_id"] = previous_id
    async with asyncio.timeout(max(0.01, timeout)):
        response = await client.with_options(
            timeout=max(0.01, timeout), max_retries=2
        ).responses.parse(**kwargs)
    return response


def require_parsed(response):
    if response.status != "completed":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        raise ModelResponseError(
            f"Model response {response.status}: {reason or 'request did not complete'}"
        )
    for item in response.output or []:
        for content in getattr(item, "content", []) or []:
            if getattr(content, "type", "") == "refusal":
                raise ModelResponseError(
                    f"Model declined the request: {content.refusal}"
                )
    if response.output_parsed is None:
        raise ModelResponseError("Model returned no structured output.")
    return response.output_parsed
