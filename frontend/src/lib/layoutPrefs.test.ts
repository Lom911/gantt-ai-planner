import { clearLayoutPrefs, loadLayoutPrefs, saveLayoutPrefs, savedGridWidth } from "./layoutPrefs";

beforeEach(() => localStorage.clear());
// A test that makes storage throw must not leave later saves silently failing.
afterEach(() => vi.restoreAllMocks());

test("saved layout survives a reload (merged per field)", () => {
  saveLayoutPrefs({ splitRatio: 0.78 });
  saveLayoutPrefs({ gridWidth: 360, columns: { text: 150, assignee: 110 } });
  saveLayoutPrefs({ zoom: "week", chatCollapsed: true });
  expect(loadLayoutPrefs()).toEqual({
    splitRatio: 0.78,
    gridWidth: 360,
    columns: { text: 150, assignee: 110 },
    zoom: "week",
    chatCollapsed: true,
  });
});

test("out-of-range or foreign values are dropped, not applied", () => {
  localStorage.setItem(
    "gantt-ai-planner:layout:v1",
    JSON.stringify({
      splitRatio: 5,
      gridWidth: -1,
      columns: { text: 9999, id: 44, evil: "x" },
      zoom: "year",
      chatCollapsed: "yes",
    }),
  );
  expect(loadLayoutPrefs()).toEqual({ columns: { id: 44 } });
});

test("corrupt JSON or a throwing storage just means defaults", () => {
  localStorage.setItem("gantt-ai-planner:layout:v1", "{not json");
  expect(loadLayoutPrefs()).toEqual({});
  const spy = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(loadLayoutPrefs()).toEqual({});
  spy.mockRestore();
  vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("quota");
  });
  expect(() => saveLayoutPrefs({ zoom: "month" })).not.toThrow();
});

test("reset forgets everything", () => {
  saveLayoutPrefs({ splitRatio: 0.6, zoom: "month" });
  clearLayoutPrefs();
  expect(loadLayoutPrefs()).toEqual({});
});

test("a grid width saved before the pencil column grows by it once; newer widths are kept as is", () => {
  expect(savedGridWidth({}, 36)).toBeUndefined();
  expect(savedGridWidth({ gridWidth: 478 }, 36)).toBe(514);
  expect(savedGridWidth({ gridWidth: 478, gridIncludesEdit: true }, 36)).toBe(478);
  saveLayoutPrefs({ gridWidth: 500, gridIncludesEdit: true });
  expect(loadLayoutPrefs()).toEqual({ gridWidth: 500, gridIncludesEdit: true });
  localStorage.setItem("gantt-ai-planner:layout:v1", JSON.stringify({ gridIncludesEdit: "yes" }));
  expect(loadLayoutPrefs()).toEqual({});
});
