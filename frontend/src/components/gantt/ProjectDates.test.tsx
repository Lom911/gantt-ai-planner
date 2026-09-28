import { render, screen } from "@testing-library/react";
import { ProjectDates } from "./ProjectDates";

test("shows the project start and end dates with its working days and all calendar days", () => {
  render(<ProjectDates start="2026-09-07" end="2026-11-19" />);
  expect(
    screen.getByText("Старт 07.09.2026 · Окончание 19.11.2026 (54 рабочих дня, всего 74 дня)"),
  ).toBeInTheDocument();
});

test("counts both ends inclusively and uses the right plural forms", () => {
  render(<ProjectDates start="2026-10-20" end="2026-10-20" />);
  expect(
    screen.getByText("Старт 20.10.2026 · Окончание 20.10.2026 (1 рабочий день, всего 1 день)"),
  ).toBeInTheDocument();
});

test("a span across the late-October DST switch is not off by one", () => {
  render(<ProjectDates start="2026-10-20" end="2026-10-30" />);
  expect(
    screen.getByText("Старт 20.10.2026 · Окончание 30.10.2026 (9 рабочих дней, всего 11 дней)"),
  ).toBeInTheDocument();
});
