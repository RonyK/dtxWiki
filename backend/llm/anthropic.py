from typing import Any

from backend.llm.base import LLMMessage, LLMRequest, LLMResponse, LLMToolCall
from backend.llm.http_provider import HTTPLLMProvider


class AnthropicLLM(HTTPLLMProvider):
    provider_name = "anthropic"
    api_key_label = "Anthropic"
    default_model = "claude-sonnet-4-6"
    default_base_url = "https://api.anthropic.com/v1/messages"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system = "\n\n".join(
            message.content for message in request.messages if message.role == "system"
        )
        messages = [
            self._message_payload(message)
            for message in request.messages
            if message.role != "system"
        ]
        payload: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if system:
            payload["system"] = system
        if request.tools:
            payload["tools"] = request.tools
        return payload

    @staticmethod
    def _message_payload(message: LLMMessage) -> dict[str, Any]:
        if message.role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id,
                        "content": message.content,
                    }
                ],
            }
        if message.role == "assistant" and message.tool_calls:
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "tool_use",
                    "id": tool_call.call_id,
                    "name": tool_call.name,
                    "input": tool_call.arguments,
                }
                for tool_call in message.tool_calls
            )
            return {"role": "assistant", "content": content}
        return {"role": message.role, "content": message.content}

    def _parse_response(self, payload: dict[str, Any]) -> LLMResponse:
        content_parts = payload.get("content", [])
        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for part in content_parts:
            if part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
            if part.get("type") == "tool_use":
                tool_calls.append(
                    LLMToolCall(
                        call_id=part.get("id"),
                        name=str(part.get("name", "")),
                        arguments=dict(part.get("input") or {}),
                    )
                )
        stop_reason = payload.get("stop_reason")
        return LLMResponse(
            provider=self.provider_name,
            model=str(payload.get("model") or self.model),
            content="\n".join(text for text in text_parts if text),
            tool_calls=tool_calls,
            stop_reason=str(stop_reason) if stop_reason is not None else None,
            raw=payload,
        )
