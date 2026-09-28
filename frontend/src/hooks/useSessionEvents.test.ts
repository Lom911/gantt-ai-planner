import { parsePlanChanged, parsePlanSource, reconnectDelay, revealMode } from "./useSessionEvents";

test("parses changed_task_ids from the plan_changed payload", () => {
  expect(parsePlanChanged(JSON.stringify({ type: "plan_changed", version: 3, changed_task_ids: [1, 2] }))).toEqual([
    1, 2,
  ]);
});

test("defaults to [] when changed_task_ids is missing", () => {
  expect(parsePlanChanged(JSON.stringify({ type: "plan_changed", version: 3 }))).toEqual([]);
});

test("defaults to [] on malformed JSON instead of throwing", () => {
  expect(parsePlanChanged("not json")).toEqual([]);
});

test("whole-plan replacements (import, reset) highlight nothing", () => {
  for (const source of ["import", "reset", "seed"]) {
    expect(parsePlanChanged(JSON.stringify({ type: "plan_changed", source, changed_task_ids: [1, 2] }))).toEqual([]);
  }
  expect(parsePlanChanged(JSON.stringify({ type: "plan_changed", source: "agent", changed_task_ids: [3] }))).toEqual([3]);
});

test("parsePlanVersion reads the version or null", async () => {
  const { parsePlanVersion } = await import("./useSessionEvents");
  expect(parsePlanVersion(JSON.stringify({ type: "plan_changed", version: 7 }))).toBe(7);
  expect(parsePlanVersion(JSON.stringify({ type: "plan_changed" }))).toBeNull();
  expect(parsePlanVersion("nope")).toBeNull();
});

test("shouldRefetchPlan skips only the event for the version already in the cache", async () => {
  const { shouldRefetchPlan } = await import("./useSessionEvents");
  expect(shouldRefetchPlan(6, 6)).toBe(false); // this tab's own apply: response already cached
  expect(shouldRefetchPlan(5, 6)).toBe(true); // someone else's change
  expect(shouldRefetchPlan(5, 4)).toBe(true); // someone else's undo: lower version, still new
  expect(shouldRefetchPlan(undefined, 6)).toBe(true);
  expect(shouldRefetchPlan(6, null)).toBe(true);
});

test("reconnect delay backs off exponentially and is capped at a minute", () => {
  expect([0, 1, 2, 3, 4, 5, 6, 10].map(reconnectDelay)).toEqual([
    2000, 4000, 8000, 16000, 32000, 60000, 60000, 60000,
  ]);
});

test("parsePlanSource reads who changed the plan, or null", () => {
  expect(parsePlanSource(JSON.stringify({ type: "plan_changed", source: "mcp" }))).toBe("mcp");
  expect(parsePlanSource(JSON.stringify({ type: "plan_changed" }))).toBeNull();
  expect(parsePlanSource("not json")).toBeNull();
});

test("the chart scrolls to the agent's changes and to this tab's own, and only notifies about the rest", () => {
  expect(revealMode("agent", false)).toBe("scroll"); // the chat asked for it, whichever tab
  expect(revealMode("user", true)).toBe("scroll"); // this tab's undo, card, new task
  expect(revealMode("user", false)).toBe("notice"); // another tab
  expect(revealMode("mcp", false)).toBe("notice"); // an external client
  expect(revealMode("mcp", true)).toBe("notice");
  expect(revealMode(null, false)).toBe("notice");
});
