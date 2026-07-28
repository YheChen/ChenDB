/**
 * How the app reaches the engine — over HTTP, or from inside the same process.
 *
 *     api.getCatalog(id)  ──▶  transport.request("/databases/…/catalog")
 *                                     │
 *                          ┌──────────┴──────────┐
 *                          ▼                     ▼
 *                    httpTransport         (a WASM transport)
 *                    fetch + WebSocket     the ASGI app, in the tab
 *
 * Every one of the 37 call sites in the app already funnelled through a single
 * `request()`, so this splits that one function in two rather than touching
 * any of them. The point is a build of the visualizer that carries the engine
 * with it — CPython compiled to WebAssembly, the same routers, the same
 * Pydantic models, no server and no network. `docs/milestone-14-transport.md`
 * has the spike that established it works.
 *
 * Two things live here that used to be spread out, and both belong to the
 * transport rather than to the app:
 *
 * * **Reconnection.** Backoff after a dropped socket is meaningless for a
 *   transport that cannot disconnect, so it moved out of `useEventStream` and
 *   into the HTTP implementation.
 * * **"Cannot reach the engine".** Also HTTP-only. An in-process transport has
 *   no such failure mode and should not have to pretend.
 */

import type { TraceRecordModel } from "@/types/api";

export const API_VERSION = "v1";
export const API_PREFIX = `/api/${API_VERSION}`;

/** Backoff for a dropped event stream: quick enough to notice a restart,
 *  slow enough not to hammer a server that is down. */
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 10_000;

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "closed"
  | "reconnecting"
  | "error";

/** What the server pushes down the event stream.
 *
 * `hello` and `error` are part of the protocol and are deliberately not acted
 * on: the first is a greeting the UI already knows, and the second arrives
 * immediately before the server closes the socket, which the reconnect path
 * handles on its own.
 */
type ServerMessage =
  | { type: "hello"; database_id: string; last_seq: number; trace_level: string }
  | { type: "events"; events: TraceRecordModel[] }
  | { type: "dropped"; count: number; total_dropped: number }
  | { type: "error"; error: string; message: string };

export type StreamHandlers = {
  onState: (state: ConnectionState) => void;
  onEvents: (events: TraceRecordModel[]) => void;
  onDropped: (count: number) => void;
};

export type Transport = {
  /** Which implementation this is, for `/health`-style reporting and tests. */
  readonly kind: "http" | "wasm";
  /** One request, with the path relative to {@link API_PREFIX}. */
  request<T>(path: string, init?: RequestInit): Promise<T>;
  /** Start the live event stream. Returns the teardown. */
  subscribe(databaseId: string, handlers: StreamHandlers): () => void;
};

/** An error carrying the server's structured envelope, when there is one. */
export class ApiRequestError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiRequestError";
  }
}

type ErrorBody = {
  detail?: { error?: string; message?: string } | string;
  error?: string;
  message?: string;
};

export function extractError(status: number, body: unknown): ApiRequestError {
  const payload = body as ErrorBody | undefined;
  const detail = payload?.detail;
  if (detail && typeof detail === "object") {
    return new ApiRequestError(
      status,
      detail.error ?? "Error",
      detail.message ?? "Request failed",
    );
  }
  if (typeof detail === "string") {
    return new ApiRequestError(status, "Error", detail);
  }
  if (payload?.message) {
    return new ApiRequestError(
      status,
      payload.error ?? "Error",
      payload.message,
    );
  }
  return new ApiRequestError(status, "Error", `Request failed (${status})`);
}

// --------------------------------------------------------------------------
// Over HTTP, to a running `python -m engine.server`
// --------------------------------------------------------------------------

export const httpTransport: Transport = {
  kind: "http",

  async request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`${API_PREFIX}${path}`, {
        ...init,
        headers: {
          ...(init?.body ? { "Content-Type": "application/json" } : {}),
          ...init?.headers,
        },
      });
    } catch {
      // A network-level failure means the engine is not running; the UI shows
      // a distinct "disconnected" state rather than a generic error.
      throw new ApiRequestError(
        0,
        "Disconnected",
        "Cannot reach the engine. Is `python -m engine.server` running?",
      );
    }

    if (response.status === 204) return undefined as T;

    const text = await response.text();
    const body = text ? JSON.parse(text) : undefined;
    if (!response.ok) throw extractError(response.status, body);
    return body as T;
  },

  subscribe(databaseId, handlers) {
    let disposed = false;
    let attempt = 0;
    let socket: WebSocket | null = null;
    let reconnectTimer: number | undefined;

    const connect = () => {
      if (disposed) return;
      handlers.onState(attempt === 0 ? "connecting" : "reconnecting");

      socket = new WebSocket(eventStreamUrl(databaseId));

      socket.onopen = () => {
        if (disposed) return;
        attempt = 0;
        handlers.onState("open");
      };

      socket.onmessage = (raw) => {
        if (disposed) return;
        let message: ServerMessage;
        try {
          message = JSON.parse(raw.data as string) as ServerMessage;
        } catch {
          return; // a malformed frame: ignore it rather than tear the stream down
        }
        if (message.type === "events") handlers.onEvents(message.events);
        else if (message.type === "dropped") handlers.onDropped(message.count);
      };

      socket.onerror = () => {
        if (!disposed) handlers.onState("error");
      };

      socket.onclose = () => {
        if (disposed) return;
        handlers.onState("closed");
        const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
        attempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (socket) {
        // Detach first: onclose would otherwise schedule a reconnect for a
        // stream we are deliberately leaving.
        socket.onclose = null;
        socket.close();
        socket = null;
      }
    };
  },
};

/** Absolute WebSocket URL for a database's live event stream. */
export function eventStreamUrl(databaseId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_PREFIX}/databases/${databaseId}/events/stream`;
}

// --------------------------------------------------------------------------
// Which one is in use
// --------------------------------------------------------------------------

let active: Transport = httpTransport;

/**
 * Swap the transport. Called once, before React mounts, by whichever entry
 * point is running — so nothing in the app ever has to ask which build it is.
 */
export function setTransport(transport: Transport): void {
  active = transport;
}

export function getTransport(): Transport {
  return active;
}
