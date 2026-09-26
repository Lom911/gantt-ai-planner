import { llmDisclaimer } from "./llmDisclaimer";

test("the chat names who actually receives the plan", () => {
  // Production goes through OpenRouter: naming only Anthropic hid the second recipient.
  expect(llmDisclaimer("openrouter")).toMatch(/OpenRouter/);
  expect(llmDisclaimer("openrouter")).toMatch(/Anthropic/);
  expect(llmDisclaimer("anthropic")).toMatch(/Anthropic/);
  expect(llmDisclaimer("anthropic")).not.toMatch(/OpenRouter/);
  expect(llmDisclaimer("fake")).toMatch(/никуда не отправляется/);
  expect(llmDisclaimer(undefined)).toMatch(/внешн/);
  for (const mode of ["openrouter", "anthropic", undefined] as const) {
    expect(llmDisclaimer(mode)).toMatch(/Не загружайте реальные персональные данные/);
  }
});
