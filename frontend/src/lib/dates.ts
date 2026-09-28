export function parseISODate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}
export function toISODate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
export function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
export function formatRu(value: Date | string): string {
  const d = typeof value === "string" ? parseISODate(value) : value;
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}
// Task history's `created_at` is a full ISO timestamp (unlike the plain "YYYY-MM-DD" dates
// elsewhere), so it's parsed with `new Date` rather than `parseISODate` and rendered with time.
export function formatRuDateTime(iso: string): string {
  return new Date(iso).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
export function nextMonday(from: Date): Date {
  const day = from.getDay(); // 0 Sun .. 6 Sat
  return addDays(from, day === 1 ? 0 : (8 - day) % 7);
}
// Counts Mon-Fri days between `start` and `end`, both inclusive. Mirrors the backend scheduler's
// workday calendar (backend/app/domain/scheduler.py): weekends never count toward a duration.
export function workdaysBetweenInclusive(start: Date, end: Date): number {
  let count = 0;
  const cursor = new Date(start);
  while (cursor <= end) {
    const day = cursor.getDay();
    if (day !== 0 && day !== 6) count++;
    cursor.setDate(cursor.getDate() + 1);
  }
  return count;
}
// Counts every calendar day (weekends included) between `start` and `end`, both inclusive.
// Math.round absorbs the 23h/25h days around DST switches.
export function calendarDaysInclusive(start: Date, end: Date): number {
  return Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
}
