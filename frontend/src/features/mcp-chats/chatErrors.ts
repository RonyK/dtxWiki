import { ApiError } from "../../shared/api/client";

/**
 * API envelopes are already mapped to stable, localized copy. Arbitrary
 * JavaScript/network errors are implementation detail and must use the
 * route-owned fallback unless the caller explicitly recognizes its message.
 */
export function safeChatErrorMessage(
  error: unknown,
  fallback: string,
  recognizedMessages: readonly string[] = [],
) {
  if (error instanceof ApiError) {
    return error.displayMessage;
  }
  if (error instanceof Error && recognizedMessages.includes(error.message)) {
    return error.message;
  }
  return fallback;
}
