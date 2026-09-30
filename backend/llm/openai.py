import json
from typing import Any

from backend.llm.base import LLMMessage, LLMRequest, LLMResponse, LLMToolCall
from backend.llm.http_provider import HTTPLLMProvider


class OpenAILLM(HTTPLLMProvider):
    provider_name = "openai"
    api_key_label = "OpenAI"
    default_model = "gpt-4o-mini"
    default_base_url = "https://api.openai.com/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [
                self._message_payload(message) for message in request.messages
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.tools:
            payload["tools"] = request.tools
        return payload

    @staticmethod
    def _message_payload(message: LLMMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.name is not None:
            payload["name"] = message.name
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.role == "assistant" and message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": tool_call.call_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": json.dumps(tool_call.arguments),
                    },
                }
                for tool_call in message.tool_calls
            ]
            if not message.content:
                payload["content"] = None
        return payload

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = [
            self._tool_call(tool_call) for tool_call in message.get("tool_calls", [])
        ]
        finish_reason = choice.get("finish_reason")
        return LLMResponse(
            provider=self.provider_name,
            model=str(payload.get("model") or self.model),
            content=str(message.get("content") or ""),
            tool_calls=tool_calls,
            stop_reason=str(finish_reason) if finish_reason is not None else None,
            raw=payload,
        )

    @staticmethod
    def _tool_call(payload: dict[str, Any]) -> LLMToolCall:
        function = payload.get("function") or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = {"raw": raw_arguments}
        return LLMToolCall(
            call_id=payload.get("id"),
            name=str(function.get("name", "")),
            arguments=arguments,
        )
