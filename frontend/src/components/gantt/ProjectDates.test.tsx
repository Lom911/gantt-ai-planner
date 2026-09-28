import { render, screen } from "@testing-library/react";
import { ProjectDates } from "./ProjectDates";

test("shows the project start and end dates with the total calendar days", () => {
  render(<ProjectDates start="2026-09-07" end="2026-11-25" />);
  expect(screen.getByText("Старт 07.09.2026 · Окончание 25.11.2026 (80 дней)")).toBeInTheDocument();
});

test("counts both ends inclusively and uses the right plural form", () => {
  render(<ProjectDates start="2026-10-20" end="2026-10-20" />);
  expect(screen.getByText("Старт 20.10.2026 · Окончание 20.10.2026 (1 день)")).toBeInTheDocument();
});

test("a span across the late-October DST switch is not off by one", () => {
  render(<ProjectDates start="2026-10-20" end="2026-10-30" />);
  expect(screen.getByText("Старт 20.10.2026 · Окончание 30.10.2026 (11 дней)")).toBeInTheDocument();
});
