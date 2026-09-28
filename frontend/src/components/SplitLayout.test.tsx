import { useEffect } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { SplitLayout } from "./SplitLayout";

// Records mounts/unmounts: ChatPanel aborts its in-flight chat request when it unmounts, so a pane
// that remounts on a tab switch (or when the window crosses the mobile breakpoint) would silently
// cancel a running agent turn.
function Probe({ name, log }: { name: string; log: string[] }) {
  useEffect(() => {
    log.push(`mount ${name}`);
    return () => {
      log.push(`unmount ${name}`);
    };
  }, [name, log]);
  return <div>{name} content</div>;
}

function setWidth(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: width });
}

beforeEach(() => localStorage.clear());
afterEach(() => setWidth(1024));

test("on a phone, switching tabs hides the inactive pane instead of unmounting it", () => {
  setWidth(390);
  const log: string[] = [];
  render(<SplitLayout left={<Probe name="chart" log={log} />} right={<Probe name="chat" log={log} />} />);
  expect(screen.getByText("chart content")).toBeVisible();
  expect(screen.getByText("chat content")).not.toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Чат" }));
  expect(screen.getByText("chat content")).toBeVisible();
  expect(screen.getByText("chart content")).not.toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: "Диаграмма" }));
  expect(screen.getByText("chart content")).toBeVisible();
  expect(log).toEqual(["mount chart", "mount chat"]);
});

test("crossing the mobile breakpoint keeps both panes mounted", () => {
  setWidth(1024);
  const log: string[] = [];
  render(<SplitLayout left={<Probe name="chart" log={log} />} right={<Probe name="chat" log={log} />} />);
  expect(screen.getByText("chat content")).toBeVisible();

  act(() => {
    setWidth(390);
    window.dispatchEvent(new Event("resize"));
  });
  expect(screen.getByRole("button", { name: "Чат" })).toBeInTheDocument();
  act(() => {
    setWidth(1024);
    window.dispatchEvent(new Event("resize"));
  });
  expect(screen.getByText("chat content")).toBeVisible();
  expect(log).toEqual(["mount chart", "mount chat"]);
});

test("by default the chat gets a fixed width and the chart takes the rest", () => {
  render(<SplitLayout left={<div>chart content</div>} right={<div>chat content</div>} />);
  expect(screen.getByText("chart content").parentElement).toHaveStyle({ width: "calc(100% - 380px)" });
});

test("on a desktop the chat collapses to a rail and comes back without remounting", () => {
  const log: string[] = [];
  render(<SplitLayout left={<Probe name="chart" log={log} />} right={<Probe name="chat" log={log} />} />);
  fireEvent.click(screen.getByRole("button", { name: "Свернуть чат" }));
  expect(screen.getByText("chat content")).not.toBeVisible();
  expect(screen.queryByRole("separator")).not.toBeInTheDocument();
  expect(screen.getByText("chart content").parentElement).not.toHaveStyle({ width: "calc(100% - 380px)" });

  fireEvent.click(screen.getByRole("button", { name: "Развернуть чат" }));
  expect(screen.getByText("chat content")).toBeVisible();
  expect(screen.getByRole("separator")).toBeInTheDocument();
  expect(log).toEqual(["mount chart", "mount chat"]);
});

test("a collapsed chat stays collapsed after a reload", () => {
  const { unmount } = render(<SplitLayout left={<div>chart content</div>} right={<div>chat content</div>} />);
  fireEvent.click(screen.getByRole("button", { name: "Свернуть чат" }));
  unmount();
  render(<SplitLayout left={<div>chart content</div>} right={<div>chat content</div>} />);
  expect(screen.getByText("chat content")).not.toBeVisible();
  expect(screen.getByRole("button", { name: "Развернуть чат" })).toBeInTheDocument();
});

test("the collapsed rail shows when the agent is working", () => {
  const { rerender } = render(<SplitLayout left={<div>chart</div>} right={<div>chat</div>} />);
  fireEvent.click(screen.getByRole("button", { name: "Свернуть чат" }));
  expect(screen.queryByTitle("Агент работает")).not.toBeInTheDocument();
  rerender(<SplitLayout left={<div>chart</div>} right={<div>chat</div>} chatBusy />);
  expect(screen.getByTitle("Агент работает")).toBeInTheDocument();
});

test("on a phone a saved collapse is ignored: the chat tab still opens the chat", () => {
  localStorage.setItem("gantt-ai-planner:layout:v1", JSON.stringify({ chatCollapsed: true }));
  setWidth(390);
  render(<SplitLayout left={<div>chart content</div>} right={<div>chat content</div>} />);
  expect(screen.queryByRole("button", { name: "Развернуть чат" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Чат" }));
  expect(screen.getByText("chat content")).toBeVisible();
});
