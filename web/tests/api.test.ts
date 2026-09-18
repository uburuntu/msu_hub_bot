import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../src/platform/api";
import { reminder, response, session } from "./fixtures";

describe("authenticated API boundary", () => {
  it("keeps init data in the header, never cookies or URL, and validates signed session shape", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(session));
    const client = new ApiClient("synthetic-private-credentials", fetcher);
    expect(await client.session("opaque launch")).toEqual(session);
    const [url, options] = fetcher.mock.calls[0]!;
    expect(url).toBe("/api/session?launch=opaque%20launch");
    expect(String(url)).not.toContain("synthetic-private");
    expect(options?.headers).toMatchObject({
      Authorization: "tma synthetic-private-credentials",
    });
    expect(options).toMatchObject({
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
  });

  it("never fetches without Telegram credentials", async () => {
    const fetcher = vi.fn<typeof fetch>();
    await expect(new ApiClient("", fetcher).list()).rejects.toMatchObject({
      status: 401,
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("posts a guarded revision and safely encodes record keys", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(response(reminder()));
    await new ApiClient("init", fetcher).action(
      reminder({ key: "a/b?c" }),
      "cancel",
    );
    expect(fetcher.mock.calls[0]![0]).toBe("/api/reminders/a%2Fb%3Fc/cancel");
    expect(JSON.parse(String(fetcher.mock.calls[0]![1]?.body))).toEqual({
      etag: "version-one",
    });
  });

  it.each([401, 409, 422, 503])(
    "preserves classified HTTP %i errors",
    async (status) => {
      const fetcher = vi
        .fn<typeof fetch>()
        .mockResolvedValue(
          response(
            { error: { code: "test", message: "Безопасное объяснение" } },
            status,
          ),
        );
      await expect(new ApiClient("init", fetcher).list()).rejects.toMatchObject(
        { status, code: "test", message: "Безопасное объяснение" },
      );
    },
  );

  it("does not leak an underlying network error or retry automatically", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockRejectedValue(new Error("synthetic-private-credentials"));
    await expect(new ApiClient("init", fetcher).list()).rejects.toThrow(
      "Связь прервалась",
    );
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    { items: null, next_cursor: null },
    { items: [reminder({ due_at: "bad" })], next_cursor: null },
    { items: [reminder({ status: "future" as never })], next_cursor: null },
  ])("rejects malformed server data", async (data) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(data));
    await expect(new ApiClient("init", fetcher).list()).rejects.toBeInstanceOf(
      ApiError,
    );
  });
});
