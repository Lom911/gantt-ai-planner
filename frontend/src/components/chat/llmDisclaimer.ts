import type { MetaResponse } from "@/api/types";

// Who receives the plan and the chat, from GET /api/meta. Production reaches Claude through
// OpenRouter, so both are named; until meta has loaded the wording stays generic.
export function llmDisclaimer(mode: MetaResponse["llm_mode"] | undefined): string {
  const warn = "Не загружайте реальные персональные данные.";
  switch (mode) {
    case "openrouter":
      return `План отправляется в LLM Claude (Anthropic) через OpenRouter. ${warn}`;
    case "anthropic":
      return `План отправляется в LLM Anthropic. ${warn}`;
    case "fake":
      return "Демо-режим без LLM: план никуда не отправляется.";
    default:
      return `План отправляется во внешнюю LLM. ${warn}`;
  }
}
