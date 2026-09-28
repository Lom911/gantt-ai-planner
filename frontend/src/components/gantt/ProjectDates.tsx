import { calendarDaysInclusive, formatRu, parseISODate } from "@/lib/dates";
import { ruPlural } from "@/lib/resourceSummary";

// A centered caption right above the chart: where the schedule is counted from (the green line
// on the timeline), where it ends, and the total span in calendar days. Lives in the chart pane,
// not the full-width toolbar, so it stays centered over the chart when the chart/chat divider is dragged.
export function ProjectDates({ start, end }: { start: string; end: string }) {
  const days = calendarDaysInclusive(parseISODate(start), parseISODate(end));
  return (
    <div
      title="Старт — дата, от которой считается план (зелёная линия на диаграмме); окончание — конец последней задачи; в скобках — всего календарных дней, включая выходные"
      className="shrink-0 border-b border-border px-3 py-1.5 text-center text-sm font-semibold"
    >
      Старт {formatRu(start)} · Окончание {formatRu(end)} ({days} {ruPlural(days, "день", "дня", "дней")})
    </div>
  );
}
