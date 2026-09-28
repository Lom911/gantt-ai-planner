import { calendarDaysInclusive, formatRu, parseISODate, workdaysBetweenInclusive } from "@/lib/dates";
import { ruPlural } from "@/lib/resourceSummary";

// A centered caption right above the chart: where the schedule is counted from (the green line
// on the timeline), where it ends, and the span both in working days (what task durations are
// counted in) and in all calendar days (what the chart shows, weekends included). Lives in the
// chart pane, not the full-width toolbar, so it stays centered over the chart when the chart/chat
// divider is dragged.
export function ProjectDates({ start, end }: { start: string; end: string }) {
  const from = parseISODate(start);
  const to = parseISODate(end);
  const days = calendarDaysInclusive(from, to);
  const workDays = workdaysBetweenInclusive(from, to);
  return (
    <div
      title="Старт — дата, от которой считается план (зелёная линия на диаграмме); окончание — конец последней задачи; в скобках — рабочие дни (пн–пт, в них считаются длительности задач) и всего календарных дней, включая выходные"
      className="shrink-0 border-b border-border px-3 py-1.5 text-center text-sm font-semibold"
    >
      Старт {formatRu(start)} · Окончание {formatRu(end)} ({workDays}{" "}
      {ruPlural(workDays, "рабочий день", "рабочих дня", "рабочих дней")}, всего {days}{" "}
      {ruPlural(days, "день", "дня", "дней")})
    </div>
  );
}
