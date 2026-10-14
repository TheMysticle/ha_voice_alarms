"""Integration with Extended OpenAI Conversation custom component (v2.x Stable)."""
import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers import llm
from homeassistant.setup import EVENT_COMPONENT_LOADED
from .const import DOMAIN, ALARM_SERVICES_PROMPT

_LOGGER = logging.getLogger(__name__)

_PATCHED = False


def _schema_to_json(schema: vol.Schema) -> dict:
    """Convert Voluptuous schema to OpenAI JSON schema without external dependencies."""
    properties = {}
    required = []

    schema_dict = getattr(schema, "schema", {})
    if isinstance(schema_dict, dict):
        for key, value in schema_dict.items():
            k_name = getattr(key, "schema", str(key))
            k_desc = getattr(key, "description", None)

            # Check if field is required
            if isinstance(key, vol.Required):
                required.append(k_name)

            prop_info = {}
            if k_desc:
                prop_info["description"] = k_desc

            # Map Python types to JSON Schema types
            if value is str or value == str:
                prop_info["type"] = "string"
            elif value is int or value == int:
                prop_info["type"] = "integer"
            elif value is bool or value == bool:
                prop_info["type"] = "boolean"
            elif value is float or value == float:
                prop_info["type"] = "number"
            elif isinstance(value, list):
                prop_info["type"] = "array"
                if value and (value[0] is str or value[0] == str):
                    prop_info["items"] = {"type": "string"}
                elif value and (value[0] is int or value[0] == int):
                    prop_info["items"] = {"type": "integer"}
                else:
                    prop_info["items"] = {"type": "string"}
            else:
                prop_info["type"] = "string"

            properties[k_name] = prop_info

    result = {"type": "object", "properties": properties}
    if required:
        result["required"] = required
    return result


async def async_patch_extended_openai(hass: HomeAssistant) -> None:
    """Attempt to patch Extended OpenAI. If it's not loaded yet, wait for it."""

    async def _apply_patch(event: Event | None = None) -> None:
        global _PATCHED
        if _PATCHED:
            return

        if event is not None and event.data.get("component") != "extended_openai_conversation":
            return

        def _import_modules():
            from custom_components.extended_openai_conversation.helpers import (
                FUNCTION_EXECUTORS,
                FunctionExecutor,
            )
            from custom_components.extended_openai_conversation.conversation import (
                ExtendedOpenAIAgentEntity,
            )
            return FUNCTION_EXECUTORS, FunctionExecutor, ExtendedOpenAIAgentEntity

        try:
            # Import in executor to avoid Home Assistant's blocking call warnings
            FUNCTION_EXECUTORS, FunctionExecutor, ExtendedOpenAIAgentEntity = (
                await hass.async_add_executor_job(_import_modules)
            )
        except ImportError as e:
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
                    if not isinstance(result, dict):
                        return {"result": result}
                    return result
                except Exception as e:
                    _LOGGER.error("Error executing %s: %s", tool_name, e)
                    return {"error": str(e)}

        FUNCTION_EXECUTORS["alarm_assistant"] = AlarmAssistantFunctionExecutor()

        # 2. Monkey-patch get_functions to auto-inject your LLM tools
        orig_get_functions = ExtendedOpenAIAgentEntity.get_functions

        def _patched_get_functions(self) -> list[dict]:
            tools = list(orig_get_functions(self))

            api = self.hass.data.get(DOMAIN, {}).get("api")
            if not api:
                return tools

            for t in api.get_enabled_tools():
                if any(existing.get("spec", {}).get("name") == t.name for existing in tools):
                    continue

                parameters = _schema_to_json(t.parameters)

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

        # 3. Monkey-patch _async_generate_prompt
        orig_async_generate_prompt = ExtendedOpenAIAgentEntity._async_generate_prompt

        def _patched_async_generate_prompt(self, raw_prompt, exposed_entities, user_input) -> str:
            base_prompt = orig_async_generate_prompt(self, raw_prompt, exposed_entities, user_input)

            api = self.hass.data.get(DOMAIN, {}).get("api")
            if api and api.get_enabled_tools():
                return f"{base_prompt}\n\n{ALARM_SERVICES_PROMPT}"

            return base_prompt

        ExtendedOpenAIAgentEntity._async_generate_prompt = _patched_async_generate_prompt

        _PATCHED = True

    await _apply_patch()

    if not _PATCHED:
        hass.bus.async_listen(EVENT_COMPONENT_LOADED, _apply_patch)