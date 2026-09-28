import { render, screen } from "@testing-library/react";
import { GanttLegend } from "./GanttLegend";

test("every legend item explains itself on hover", () => {
  render(<GanttLegend />);
  for (const label of ["Обычная задача", "Критический путь", "Перегрузка", "Изменено", "Старт проекта", "Сегодня", "Выходные"]) {
    expect(screen.getByText(label).closest("[title]")?.getAttribute("title")).toBeTruthy();
  }
  expect(screen.getByText("Критический путь").closest("[title]")?.getAttribute("title")).toMatch(/резерв 0/);
  expect(screen.getByText("Выходные").closest("[title]")?.getAttribute("title")).toMatch(/рабочие дни/);
});
