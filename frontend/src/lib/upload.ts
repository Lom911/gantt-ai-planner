// Must match the backend's MAX_UPLOAD_MB (backend/app/config.py, default 2): checking in the
// browser saves uploading a file the server will reject anyway, and says why up front.
export const MAX_UPLOAD_MB = 2;

export function uploadSizeError(bytes: number): string | null {
  return bytes > MAX_UPLOAD_MB * 1024 * 1024
    ? `Файл больше ${MAX_UPLOAD_MB} МБ — сервер его не примет. Уменьшите файл (например, удалите лишние листы).`
    : null;
}
