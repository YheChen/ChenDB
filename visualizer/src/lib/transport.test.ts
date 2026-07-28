/**
 * The seam, exercised by a transport that is not HTTP.
 *
 * An abstraction with one implementation is a claim, not a fact. These tests
 * swap in a transport that never touches `fetch` or `WebSocket` and drive the
 * real `api` object and the real event-stream hook through it — which is
 * exactly what the WASM build will do, minus a Python interpreter.
 *
 * If a future change reaches around `getTransport()` and calls `fetch`
 * directly, `assertNoNetwork` below fails rather than the WASM build silently
 * losing an endpoint.
 */

import { renderHook, act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import {
  ApiRequestError,
  extractError,
  getTransport,
  httpTransport,
  setTransport,
  type StreamHandlers,
  type Transport,
} from "./transport";
import { useEventStream } from "@/hooks/useEventStream";
import type { TraceRecordModel } from "@/types/api";

/** A transport with no network under it at all. */
function fakeTransport(
  routes: Record<string, unknown> = {},
): Transport & { seen: string[]; push: StreamHandlers | null } {
  const state = {
    kind: "wasm" as const,
    seen: [] as string[],
    push: null as StreamHandlers | null,

    async request<T>(path: string, init?: RequestInit): Promise<T> {
      state.seen.push(`${init?.method ?? "GET"} ${path}`);
      if (path in routes) return routes[path] as T;
      throw new ApiRequestError(404, "NotFound", `no fake route for ${path}`);
    },

    subscribe(_databaseId: string, handlers: StreamHandlers) {
      state.push = handlers;
      handlers.onState("open");
      return () => {
        state.push = null;
      };
    },
  };
  return state;
}

let assertNoNetwork: () => void;

beforeEach(() => {
  const fetchSpy = vi.spyOn(globalThis, "fetch");
  const socketSpy = vi.spyOn(globalThis, "WebSocket" as never);
  assertNoNetwork = () => {
    expect(fetchSpy, "the transport was bypassed and fetch was called").not.toHaveBeenCalled();
    expect(socketSpy, "the transport was bypassed and a socket was opened").not.toHaveBeenCalled();
  };
});

afterEach(() => {
  setTransport(httpTransport);
  vi.restoreAllMocks();
});

describe("the active transport", () => {
  it("is HTTP unless something says otherwise", () => {
    expect(getTransport().kind).toBe("http");
  });

  it("can be swapped, and the api client follows it", async () => {
    const fake = fakeTransport({
      "/health": { engine_version: "1.3.0", milestone: 13 },
    });
    setTransport(fake);

    const health = await api.health();

    expect(health.engine_version).toBe("1.3.0");
    expect(fake.seen).toEqual(["GET /health"]);
    assertNoNetwork();
  });

  it("carries the method and body of a write", async () => {
    const fake = fakeTransport({ "/databases": { database_id: "demo" } });
    setTransport(fake);

    await api.createDatabase({ database_id: "demo", page_size: 4096 });

    expect(fake.seen).toEqual(["POST /databases"]);
    assertNoNetwork();
  });

  it("passes the session through, because two consoles depend on it", async () => {
    const fake = fakeTransport({
      "/databases/demo/query?session=alice": [{ message: "ok" }],
    });
    setTransport(fake);

    await api.runQuery("demo", "SELECT 1 FROM t", undefined, "alice");

    expect(fake.seen[0]).toContain("session=alice");
    assertNoNetwork();
  });

  it("surfaces an error from a transport that never saw a status code", async () => {
    setTransport(fakeTransport());
    await expect(api.getCatalog("demo")).rejects.toThrow(ApiRequestError);
  });
});

describe("the event stream over a transport that cannot disconnect", () => {
  const record = (seq: number): TraceRecordModel =>
    ({ seq, event_type: "PageRead", category: "storage" }) as TraceRecordModel;

  it("delivers events without a WebSocket anywhere", async () => {
    const fake = fakeTransport();
    setTransport(fake);
    vi.useFakeTimers();

    try {
      const { result } = renderHook(() => useEventStream("demo"));
      expect(result.current.connection).toBe("open");

      act(() => {
        fake.push?.onEvents([record(1), record(2)]);
        // The hook batches on a timer rather than rendering per event.
        vi.advanceTimersByTime(200);
      });

      expect(result.current.events).toHaveLength(2);
      expect(result.current.totalReceived).toBe(2);
      assertNoNetwork();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports what the producer dropped", async () => {
    const fake = fakeTransport();
    setTransport(fake);

    const { result } = renderHook(() => useEventStream("demo"));
    act(() => fake.push?.onDropped(17));

    await waitFor(() => expect(result.current.droppedByServer).toBe(17));
  });

  it("unsubscribes on unmount, so a swapped transport is not left holding a callback", () => {
    const fake = fakeTransport();
    setTransport(fake);

    const { unmount } = renderHook(() => useEventStream("demo"));
    expect(fake.push).not.toBeNull();
    unmount();
    expect(fake.push).toBeNull();
  });
});

describe("the error envelope", () => {
  // Unchanged by the split, and worth pinning: every panel's error notice
  // reads `code` and `message` off whatever comes out of here.
  it("unwraps the server's detail object", () => {
    const error = extractError(422, {
      detail: { error: "BindingError", message: "no column named 'nope'" },
    });
    expect(error.code).toBe("BindingError");
    expect(error.message).toBe("no column named 'nope'");
  });

  it("accepts a bare string detail", () => {
    expect(extractError(404, { detail: "no such page" }).message).toBe("no such page");
  });

  it("falls back to the status when the body says nothing useful", () => {
    expect(extractError(500, {}).message).toBe("Request failed (500)");
  });
});
