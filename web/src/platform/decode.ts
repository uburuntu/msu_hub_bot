import { ApiError } from "./errors";

export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value as Record<string, unknown>;
}

export function string(value: unknown): string {
  if (typeof value !== "string")
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value;
}

export function integer(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value))
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value;
}

export function boolean(value: unknown): boolean {
  if (typeof value !== "boolean")
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value;
}

export function array<T>(value: unknown, decode: (item: unknown) => T): T[] {
  if (!Array.isArray(value))
    throw new ApiError("protocol", "Не удалось прочитать ответ сервера.", 503);
  return value.map(decode);
}

export function stamp(value: unknown): string {
  const result = string(value);
  if (!Number.isFinite(Date.parse(result)))
    throw new ApiError("protocol", "Не удалось прочитать дату.", 503);
  return result;
}
