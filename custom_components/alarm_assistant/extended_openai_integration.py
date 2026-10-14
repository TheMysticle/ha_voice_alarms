"""Integration with Extended OpenAI Conversation custom component."""
import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.loader import async_get_integration
from .const import DOMAIN, ALARM_SERVICES_PROMPT

_LOGGER = logging.getLogger(__name__)

async def async_patch_extended_openai(hass: HomeAssistant) -> None:
    """Patch Extended OpenAI Conversation to inject Alarm Assistant tools automatically."""
    
    # 1. Force Home Assistant to resolve the Extended OpenAI component
    # This guarantees Python can find the custom_components.extended_openai_conversation path.
    try:
        await async_get_integration(hass, "extended_openai_conversation")
    except Exception:
        # If the integration truly isn't installed, quietly exit.
        return

    _LOGGER.warning("Attempting to patch Extended OpenAI Conversation...")

    try:
        from custom_components.extended_openai_conversation.functions import FUNCTIONS, Function
        from custom_components.extended_openai_conversation.conversation import ExtendedOpenAIAgentEntity
        from voluptuous_openapi import convert
    except ImportError as e:
        _LOGGER.warning("FAILED to patch Extended OpenAI: Modules missing. Error: %s", e)
        return
    except Exception as e:
        _LOGGER.error("FAILED to patch Extended OpenAI: Unexpected error: %s", e)
        return

    if "alarm_assistant" in FUNCTIONS:
        _LOGGER.warning("Extended OpenAI was already patched.")
        return

    _LOGGER.warning("SUCCESS: Patching Extended OpenAI Conversation to add Voice Alarm Assistant tools!")

    # 2. Custom Function type to translate Extended OpenAI format -> HA LLM Tool format
    class AlarmAssistantFunction(Function):
        def __init__(self):
            super().__init__(vol.Schema({
                vol.Required("tool_name"): str
            }))

        async def execute(self, hass_internal: HomeAssistant, function_config: dict, arguments: dict, llm_context, exposed_entities):
            tool_name = function_config["tool_name"]
            api = hass_internal.data.get(DOMAIN, {}).get("api")
            if not api:
                return {"error": "Voice Alarm Assistant API not initialized"}

            tools = api.get_enabled_tools()
            target_tool = next((t for t in tools if t.name == tool_name), None)

            if not target_tool:
                return {"error": f"Tool {tool_name} not found"}

            tool_input = llm.ToolInput(tool_name=tool_name, tool_args=arguments)

            try:
                result = await target_tool.async_call(hass_internal, tool_input, llm_context)
                if not isinstance(result, dict):
                    return {"result": result}
                return result
            except Exception as e:
                _LOGGER.error("Error executing %s: %s", tool_name, e)
                return {"error": str(e)}

    # Register the function
    FUNCTIONS["alarm_assistant"] = AlarmAssistantFunction()

    # 3. Auto-inject the tools without YAML config
    orig_get_function_tools = ExtendedOpenAIAgentEntity._get_function_tools

    def _patched_get_function_tools(self) -> list[dict]:
        tools = list(orig_get_function_tools(self))
        api = self.hass.data.get(DOMAIN, {}).get("api")
        if not api:
            return tools

        for t in api.get_enabled_tools():
            if any(existing.get("spec", {}).get("name") == t.name for existing in tools):
                continue

            parameters = convert(t.parameters)
            if "type" not in parameters:
                parameters["type"] = "object"
            if "properties" not in parameters:
                parameters["properties"] = {}

            tool_dict = {
                "spec": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": parameters,
                },
                "function": FUNCTIONS["alarm_assistant"].validate_schema({
                    "type": "alarm_assistant",
                    "tool_name": t.name
                })
            }
            tools.append(tool_dict)
        return tools

    ExtendedOpenAIAgentEntity._get_function_tools = _patched_get_function_tools

    # 4. Inform the LLM HOW to use the alarms
    orig_build_system_prompt = ExtendedOpenAIAgentEntity._build_system_prompt

    def _patched_build_system_prompt(self, exposed_entities, llm_context, user_input) -> str:
        base_prompt = orig_build_system_prompt(self, exposed_entities, llm_context, user_input)
        api = self.hass.data.get(DOMAIN, {}).get("api")
        if api and api.get_enabled_tools():
            return f"{base_prompt}\n\n{ALARM_SERVICES_PROMPT}"
        return base_prompt

    ExtendedOpenAIAgentEntity._build_system_prompt = _patched_build_system_prompt