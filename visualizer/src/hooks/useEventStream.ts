/**
 * Live diagnostic events over WebSocket.
 *
 * Three things this hook has to get right:
 *
 * 1. **Bounded memory.** The engine can emit thousands of events per second at
 *    VERBOSE. The buffer keeps only the newest `capacity` and reports how many
 *    it discarded, mirroring the server's own ring buffer.
 * 2. **Batched rendering.** One React state update per event would make the UI
 *    slower than the engine it is watching. Incoming events are staged in a ref
 *    and flushed on an interval.
 * 3. **Honest reconnection.** A dropped socket reconnects with backoff, and the
 *    gap is visible in the UI rather than papered over.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { eventStreamUrl } from "@/lib/api";
import type { TraceRecordModel } from "@/types/api";

export type ConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed"
  | "error";

export interface EventStreamState {
  events: TraceRecordModel[];
  connection: ConnectionState;
  droppedByServer: number;
  droppedByClient: number;
  totalReceived: number;
  clear: () => void;
}

const DEFAULT_CAPACITY = 2_000;
const FLUSH_INTERVAL_MS = 100;
const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 10_000;

type ServerMessage =
  | { type: "hello"; database_id: string; last_seq: number; trace_level: string }
  | { type: "events"; events: TraceRecordModel[] }
  | { type: "dropped"; count: number; total_dropped: number }
  | { type: "error"; error: string; message: string };

export function useEventStream(
  databaseId: string | null,
  options: { capacity?: number; paused?: boolean } = {},
): EventStreamState {
  const capacity = options.capacity ?? DEFAULT_CAPACITY;
  const paused = options.paused ?? false;

  const [events, setEvents] = useState<TraceRecordModel[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const [droppedByServer, setDroppedByServer] = useState(0);
  const [droppedByClient, setDroppedByClient] = useState(0);
  const [totalReceived, setTotalReceived] = useState(0);

  const pending = useRef<TraceRecordModel[]>([]);
  const socketRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;

  const clear = useCallback(() => {
    pending.current = [];
    setEvents([]);
    setDroppedByServer(0);
    setDroppedByClient(0);
    setTotalReceived(0);
  }, []);

  // Flush staged events on a timer rather than per message.
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (pending.current.length === 0) return;
      const batch = pending.current;
      pending.current = [];
      setEvents((current) => {
        const merged = current.concat(batch);
        if (merged.length <= capacity) return merged;
        const overflow = merged.length - capacity;
        setDroppedByClient((count) => count + overflow);
        return merged.slice(overflow);
      });
      setTotalReceived((count) => count + batch.length);
    }, FLUSH_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [capacity]);

  useEffect(() => {
    if (!databaseId) {
      setConnection("idle");
      return;
    }

    let disposed = false;
    let reconnectTimer: number | undefined;

    const connect = () => {
      if (disposed) return;
      setConnection(attemptRef.current === 0 ? "connecting" : "reconnecting");

      const socket = new WebSocket(eventStreamUrl(databaseId));
      socketRef.current = socket;

      socket.onopen = () => {
        if (disposed) return;
        attemptRef.current = 0;
        setConnection("open");
      };

      socket.onmessage = (raw) => {
        if (disposed || pausedRef.current) return;
        let message: ServerMessage;
        try {
          message = JSON.parse(raw.data as string) as ServerMessage;
        } catch {
          return; // malformed frame: ignore rather than tear down the stream
        }
        if (message.type === "events") {
          pending.current.push(...message.events);
        } else if (message.type === "dropped") {
          setDroppedByServer((count) => count + message.count);
        }
      };

      socket.onerror = () => {
        if (!disposed) setConnection("error");
      };

      socket.onclose = () => {
        if (disposed) return;
        setConnection("closed");
        // Exponential backoff, capped: a server restart should be picked up
        // quickly, but a server that is down must not be hammered.
        const delay = Math.min(
          RECONNECT_MAX_MS,
          RECONNECT_BASE_MS * 2 ** attemptRef.current,
        );
        attemptRef.current += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer) window.clearTimeout(reconnectTimer);
      const socket = socketRef.current;
      socketRef.current = null;
      // onclose would otherwise schedule a reconnect for a stream we are
      // deliberately leaving.
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [databaseId]);

  return {
    events,
    connection,
    droppedByServer,
    droppedByClient,
    totalReceived,
    clear,
  };
}
