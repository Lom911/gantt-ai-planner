import { useCallback, useEffect, useRef, useState } from "react";

// Spec §12: changed tasks are highlighted for 2 s. The highlight must then go away: it takes
// priority over the critical-path/overload colours (see toSvarTasks), so a highlight that
// never cleared would hide them after the first edit.
export const HIGHLIGHT_MS = 2000;
// A change the chart had to scroll to stays highlighted longer: the eye needs a moment to find it.
export const REVEAL_HIGHLIGHT_MS = 5000;

// Returns the currently highlighted ids and `flash(ids, ms?)`, which replaces them and restarts
// the clear timer (a new change arriving mid-highlight gets its own full HIGHLIGHT_MS, or `ms`).
export function useFlashHighlight(
  durationMs: number = HIGHLIGHT_MS,
): readonly [ReadonlySet<number>, (ids: Iterable<number>, ms?: number) => void] {
  const [ids, setIds] = useState<ReadonlySet<number>>(() => new Set());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flash = useCallback(
    (next: Iterable<number>, ms: number = durationMs) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      const set = new Set(next);
      setIds(set);
      if (set.size === 0) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        setIds(new Set());
      }, ms);
    },
    [durationMs],
  );

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  return [ids, flash] as const;
}
