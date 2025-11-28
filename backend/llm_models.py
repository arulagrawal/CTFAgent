"""
LLM Model Registry

This module consolidates information about each LLM model including:
- Display name
- API string (identifier used in API calls)
- Pricing information (per 1M input/output tokens)
"""

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class LLMModelInfo:
    """Information about an LLM model."""
    name: str  # Display name
    api_string: str  # API identifier string
    price_input_per_1m: float  # Price per 1M input tokens in USD
    price_output_per_1m: float  # Price per 1M output tokens in USD


# Model registry mapping model names (as used in the codebase) to their information
MODEL_REGISTRY: Dict[str, LLMModelInfo] = {
    "gpt-5-mini": LLMModelInfo(
        name="GPT-5 Mini",
        api_string="gpt-5-mini",
        price_input_per_1m=0.250,
        price_output_per_1m=2.000,
    ),
    "gpt-5": LLMModelInfo(
        name="GPT-5",
        api_string="gpt-5",
        price_input_per_1m=1.250,
        price_output_per_1m=10.000,
    ),
    "gpt-5.1": LLMModelInfo(
        name="GPT-5.1",
        api_string="gpt-5.1",
        price_input_per_1m=1.250,
        price_output_per_1m=10.000,
    ),
}


def get_model_info(model_name: str) -> Optional[LLMModelInfo]:
    """
    Get model information by model name.
    
    Args:
        model_name: The model name (e.g., "gpt-5-mini", "gpt-5", "gpt-5.1")
        
    Returns:
        LLMModelInfo if found, None otherwise
    """
    # Normalize the model name (lowercase, handle variations)
    normalized = model_name.lower().strip().replace(" ", "").replace("_", "-")
    return MODEL_REGISTRY.get(normalized)


def get_api_string(model_name: str) -> str:
    """
    Get the API string for a model name.
    
    Args:
        model_name: The model name
        
    Returns:
        The API string, or the original model_name if not found
    """
    model_info = get_model_info(model_name)
    if model_info:
        return model_info.api_string
    # Fallback to original name if not found
    return model_name


def get_available_models() -> Dict[str, LLMModelInfo]:
    """
    Get all available models.
    
    Returns:
        Dictionary mapping model names to their info
    """
    return MODEL_REGISTRY.copy()


def is_valid_model(model_name: str) -> bool:
    """
    Check if a model name is valid.
    
    Args:
        model_name: The model name to check
        
    Returns:
        True if valid, False otherwise
    """
    return get_model_info(model_name) is not None

