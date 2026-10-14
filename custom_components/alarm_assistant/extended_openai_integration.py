"""Integration with Extended OpenAI Conversation custom component (v2.x Stable)."""
import logging

from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers import llm
from homeassistant.setup import EVENT_COMPONENT_LOADED
from .const import DOMAIN, ALARM_SERVICES_PROMPT

_LOGGER = logging.getLogger(__name__)

# Track if we've successfully patched to avoid doing it twice
_PATCHED = False

async def async_patch_extended_openai(hass: HomeAssistant) -> None:
    """Attempt to patch Extended OpenAI. If it's not loaded yet, wait for it."""
    
    async def _apply_patch(event: Event | None = None) -> None:
        global _PATCHED
        if _PATCHED:
            return
            
        # If triggered by an event, check if the component loaded was the one we want
        if event is not None and event.data.get("component") != "extended_openai_conversation":
            return

        try:
            # v2.x architecture imports
            from custom_components.extended_openai_conversation.helpers import FUNCTION_EXECUTORS, FunctionExecutor
            from custom_components.extended_openai_conversation.conversation import ExtendedOpenAIAgentEntity
            from voluptuous_openapi import convert
            import voluptuous as vol
        except ImportError as e:
            # If we were explicitly triggered by the event, the module should exist.
            if event is not None:
                _LOGGER.error("Extended OpenAI loaded, but modules are missing: %s", e)
            return
        except Exception as e:
            _LOGGER.error("FAILED to patch Extended OpenAI: %s", e)
            return

        if "alarm_assistant" in FUNCTION_EXECUTORS:
            _PATCHED = True
            return

        _LOGGER.warning("SUCCESS: Patching Extended OpenAI Conversation (Stable v2.x) to add Voice Alarm Assistant tools!")

        # 1. Create a custom FunctionExecutor for v2.x
        class AlarmAssistantFunctionExecutor(FunctionExecutor):
            def __init__(self):
                super().__init__(vol.Schema({
                    vol.Required("tool_name"): str
                }))

            async def execute(self, hass_internal: HomeAssistant, function: dict, arguments: dict, user_input, exposed_entities):
                tool_name = function.get("tool_name")

                api = hass_internal.data.get(DOMAIN, {}).get("api")
                if not api:
                    return {"error": "Voice Alarm Assistant API not initialized"}

                tools = api.get_enabled_tools()
                target_tool = next((t for t in tools if t.name == tool_name), None)

                if not target_tool:
                    return {"error": f"Tool {tool_name} not found"}

                # Construct an LLMContext from user_input for HA's built-in tool system
                llm_context = llm.LLMContext(
                    platform=DOMAIN,
                    context=user_input.context,
                    user_prompt=user_input.text,
                    language=user_input.language,
                    assist_custom_language=user_input.language,
                    device_id=user_input.device_id,
                )

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

        # Register the new executor into Extended OpenAI's v2.x dictionary
        FUNCTION_EXECUTORS["alarm_assistant"] = AlarmAssistantFunctionExecutor()

        # 2. Monkey-patch get_functions to auto-inject your LLM tools
        orig_get_functions = ExtendedOpenAIAgentEntity.get_functions

        def _patched_get_functions(self) -> list[dict]:
            # Get whatever tools the user manually configured
            tools = list(orig_get_functions(self))
            
            api = self.hass.data.get(DOMAIN, {}).get("api")
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
                    "function": {
                        "type": "alarm_assistant",
                        "tool_name": t.name
                    }
                }
                tools.append(tool_dict)

            return tools

        ExtendedOpenAIAgentEntity.get_functions = _patched_get_functions

        # 3. Monkey-patch _async_generate_prompt to inform the LLM *how* to use the alarms
        orig_async_generate_prompt = ExtendedOpenAIAgentEntity._async_generate_prompt

        def _patched_async_generate_prompt(self, raw_prompt, exposed_entities, user_input) -> str:
            base_prompt = orig_async_generate_prompt(self, raw_prompt, exposed_entities, user_input)
            
            api = self.hass.data.get(DOMAIN, {}).get("api")
            if api and api.get_enabled_tools():
                return f"{base_prompt}\n\n{ALARM_SERVICES_PROMPT}"
                
            return base_prompt

        ExtendedOpenAIAgentEntity._async_generate_prompt = _patched_async_generate_prompt
        
        _PATCHED = True

    # 1. Run immediately in case Extended OpenAI is already loaded
    await _apply_patch()
    
    # 2. If it wasn't loaded, listen for the event bus to tell us when it is!
    if not _PATCHED:
        hass.bus.async_listen(EVENT_COMPONENT_LOADED, _apply_patch)