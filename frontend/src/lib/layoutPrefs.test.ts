import { clearLayoutPrefs, loadLayoutPrefs, saveLayoutPrefs } from "./layoutPrefs";

beforeEach(() => localStorage.clear());

test("saved layout survives a reload (merged per field)", () => {
  saveLayoutPrefs({ splitRatio: 0.78 });
  saveLayoutPrefs({ gridWidth: 360, columns: { text: 150, assignee: 110 } });
  saveLayoutPrefs({ zoom: "week" });
  expect(loadLayoutPrefs()).toEqual({ splitRatio: 0.78, gridWidth: 360, columns: { text: 150, assignee: 110 }, zoom: "week" });
});

test("out-of-range or foreign values are dropped, not applied", () => {
  localStorage.setItem(
    "gantt-ai-planner:layout:v1",
    JSON.stringify({ splitRatio: 5, gridWidth: -1, columns: { text: 9999, id: 44, evil: "x" }, zoom: "year" }),
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
