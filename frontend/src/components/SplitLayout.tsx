import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/utils";
import { loadLayoutPrefs, saveLayoutPrefs } from "@/lib/layoutPrefs";

const MOBILE_BREAKPOINT = 768;
const LEFT_MIN_WIDTH = 360;
const RIGHT_MIN_WIDTH = 320;
const DEFAULT_LEFT_RATIO = 0.7;

export function SplitLayout({ left, right }: { left: ReactNode; right: ReactNode }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [isMobile, setIsMobile] = useState(() => window.innerWidth < MOBILE_BREAKPOINT);
  const [tab, setTab] = useState<"chart" | "chat">("chart");
  const [leftWidth, setLeftWidth] = useState<number | null>(null);
  // The split the user dragged to last time (as a share of the width, so it fits any window).
  const [savedRatio] = useState(() => loadLayoutPrefs().splitRatio);
  const lastWidth = useRef<number | null>(null);
  const dragging = useRef(false);

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const clamp = useCallback((width: number, total: number) => {
    const max = Math.max(LEFT_MIN_WIDTH, total - RIGHT_MIN_WIDTH);
    return Math.min(Math.max(width, LEFT_MIN_WIDTH), max);
  }, []);

  const onPointerMove = useCallback(
    (e: PointerEvent) => {
      if (!dragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const width = clamp(e.clientX - rect.left, rect.width);
      lastWidth.current = width;
      setLeftWidth(width);
    },
    [clamp],
  );

  const stopDragging = useCallback(() => {
    dragging.current = false;
    window.removeEventListener("pointermove", onPointerMove);
    const total = containerRef.current?.getBoundingClientRect().width;
    if (lastWidth.current != null && total) saveLayoutPrefs({ splitRatio: lastWidth.current / total });
  }, [onPointerMove]);

  const startDragging = useCallback(() => {
    dragging.current = true;
    window.addEventListener("pointermove", onPointerMove);
    // `once` fires and detaches automatically, so `stopDragging` never needs to remove it itself.
    window.addEventListener("pointerup", stopDragging, { once: true });
  }, [onPointerMove, stopDragging]);

  useEffect(() => () => stopDragging(), [stopDragging]);

  // One element tree for both layouts, with both panes always mounted: on a phone the inactive
  // tab's pane is only `hidden`. Unmounting it would abort work in progress: ChatPanel cancels
  // its streaming request on unmount, which kills a running agent turn (the natural phone flow
  // is to send a message, then switch to the chart to watch it change). Keeping the children at
  // the same positions also means crossing the breakpoint (rotation, resize) remounts nothing.
  const tabClass = (active: boolean) =>
    cn(
      "flex-1 px-3 py-2 text-sm font-medium",
      active ? "border-b-2 border-primary text-foreground" : "text-muted-foreground",
    );

  return (
    <div ref={containerRef} className={cn("flex h-full min-h-0", isMobile && "flex-col")}>
      {isMobile && (
        <div className="flex border-b border-border">
          <button type="button" className={tabClass(tab === "chart")} onClick={() => setTab("chart")}>
            Диаграмма
          </button>
          <button type="button" className={tabClass(tab === "chat")} onClick={() => setTab("chat")}>
            Чат
          </button>
        </div>
      )}
      <div
        className={cn("min-h-0 overflow-auto", isMobile && "flex-1")}
        style={
          isMobile
            ? undefined
            : {
                width: leftWidth ?? `${(savedRatio ?? DEFAULT_LEFT_RATIO) * 100}%`,
                // A saved share can land outside the pane limits on a smaller window.
                minWidth: LEFT_MIN_WIDTH,
                maxWidth: `calc(100% - ${RIGHT_MIN_WIDTH}px)`,
                flexShrink: 0,
              }
        }
        hidden={isMobile && tab !== "chart"}
      >
        {left}
      </div>
      {!isMobile && (
        <div
          role="separator"
          aria-orientation="vertical"
          className="w-1 shrink-0 cursor-col-resize bg-border hover:bg-ring"
          onPointerDown={startDragging}
        />
      )}
      <div className="min-h-0 flex-1 overflow-auto" hidden={isMobile && tab !== "chat"}>
        {right}
      </div>
    </div>
  );
}
