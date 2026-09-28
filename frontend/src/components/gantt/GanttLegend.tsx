import { HIGHLIGHT_MS } from "@/hooks/useFlashHighlight";

// Colors reuse the CSS custom properties gantt.css already defines for bar styling (`:root` /
// `.dark`), so the legend swatches always match what's actually painted on the bars. Each item
// explains itself on hover: "критический путь" and "перегрузка" are planning jargon.
const ITEMS = [
  {
    color: "var(--gantt-task)",
    label: "Обычная задача",
    hint: "Задача с запасом времени: небольшая задержка не сдвинет окончание проекта.",
  },
  {
    color: "var(--gantt-critical)",
    label: "Критический путь",
    hint: "Цепочка задач без запаса времени (резерв 0 дн.): задержка любой из них сдвигает окончание всего проекта.",
  },
  {
    color: "var(--gantt-conflict)",
    label: "Перегрузка",
    hint: "У исполнителя в эти дни несколько задач одновременно. С какими именно — в карточке задачи и в панели «Загрузка».",
  },
  {
    color: "var(--gantt-changed)",
    label: "Изменено",
    hint: `Задачи, которые только что изменились (чат, карточка, перетаскивание, MCP, отмена), подсвечиваются на ${HIGHLIGHT_MS / 1000} с.`,
  },
];

// The green swatch matches the start line drawn on the timeline, the tinted one the today column;
// the dates themselves are in the caption above the chart.
export function GanttLegend() {
  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
      {ITEMS.map((item) => (
        <span key={item.label} title={item.hint} className="inline-flex cursor-help items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: item.color }} aria-hidden="true" />
          {item.label}
        </span>
      ))}
      <span
        title="Дата, от которой считается план: задачи без предшественников начинаются в этот день."
        className="inline-flex cursor-help items-center gap-1.5"
      >
        <span className="h-3 w-0.5" style={{ background: "var(--gantt-start)" }} aria-hidden="true" />
        Старт проекта
      </span>
      <span
        title="Столбец текущего дня. Нажмите на число в шкале дней, чтобы так же выделить любой другой день; повторное нажатие снимает выделение."
        className="inline-flex cursor-help items-center gap-1.5"
      >
        <span
          className="h-3 w-2.5 rounded-sm border border-border"
          style={{ background: "var(--gantt-today)" }}
          aria-hidden="true"
        />
        Сегодня
      </span>
    </div>
  );
}
