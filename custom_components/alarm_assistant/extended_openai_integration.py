"""Integration with Extended OpenAI Conversation custom component."""
import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from .const import DOMAIN, ALARM_SERVICES_PROMPT

_LOGGER = logging.getLogger(__name__)

def patch_extended_openai(hass: HomeAssistant) -> None:
    """Patch Extended OpenAI Conversation to inject Alarm Assistant tools automatically."""
    
    _LOGGER.warning("Attempting to patch Extended OpenAI Conversation...")

    try:
        from custom_components.extended_openai_conversation.functions import FUNCTIONS, Function
        from custom_components.extended_openai_conversation.conversation import ExtendedOpenAIAgentEntity
        from voluptuous_openapi import convert
    except ImportError as e:
        _LOGGER.warning("FAILED to patch Extended OpenAI: Could not import required modules. Error: %s", e)
        return
    except Exception as e:
        _LOGGER.error("FAILED to patch Extended OpenAI due to unexpected error: %s", e)
        return

    if "alarm_assistant" in FUNCTIONS:
        _LOGGER.warning("Extended OpenAI was already patched by Alarm Assistant.")
        return

    _LOGGER.warning("SUCCESS: Patching Extended OpenAI Conversation to add Voice Alarm Assistant tools!")

    # 1. Create a custom Function type to translate Extended OpenAI format -> HA LLM Tool format
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
                # Extended OpenAI expects dict responses
                if not isinstance(result, dict):
                    return {"result": result}
                return result
            except Exception as e:
                _LOGGER.error("Error executing %s: %s", tool_name, e)
                return {"error": str(e)}

    # Register the new function type
    FUNCTIONS["alarm_assistant"] = AlarmAssistantFunction()

    # 2. Monkey-patch _get_function_tools to auto-inject your LLM tools without YAML config
    orig_get_function_tools = ExtendedOpenAIAgentEntity._get_function_tools

    def _patched_get_function_tools(self) -> list[dict]:
        # Get whatever tools the user manually configured
        tools = list(orig_get_function_tools(self))
        
        api = hass.data.get(DOMAIN, {}).get("api")
        if not api:
            return tools

        for t in api.get_enabled_tools():
            # Skip if user already defined a tool with the same name manually
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

    # 3. Monkey-patch _build_system_prompt to inform the LLM *how* to use the alarms
    orig_build_system_prompt = ExtendedOpenAIAgentEntity._build_system_prompt

    def _patched_build_system_prompt(self, exposed_entities, llm_context, user_input) -> str:
        base_prompt = orig_build_system_prompt(self, exposed_entities, llm_context, user_input)
        
        api = hass.data.get(DOMAIN, {}).get("api")
        if api and api.get_enabled_tools():
            return f"{base_prompt}\n\n{ALARM_SERVICES_PROMPT}"
            
        return base_prompt

    ExtendedOpenAIAgentEntity._build_system_prompt = _patched_build_system_prompt