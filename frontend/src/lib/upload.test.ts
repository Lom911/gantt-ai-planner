import { MAX_UPLOAD_MB, uploadSizeError } from "./upload";

test("files over the server's upload limit are rejected in the browser", () => {
  const limit = MAX_UPLOAD_MB * 1024 * 1024;
  expect(uploadSizeError(limit)).toBeNull();
  expect(uploadSizeError(limit + 1)).toMatch(/больше 2 МБ/);
  expect(uploadSizeError(0)).toBeNull();
});
