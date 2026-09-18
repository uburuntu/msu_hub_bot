import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.reject(new Error("Unmocked network request"))),
  );
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
