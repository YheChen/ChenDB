/**
 * Live diagnostic events, buffered for a React tree.
 *
 * Three things this hook has to get right:
 *
 * 1. **Bounded memory.** The engine can emit thousands of events per second at
 *    VERBOSE. The buffer keeps only the newest `capacity` and reports how many
 *    it discarded, mirroring the server's own ring buffer.
 * 2. **Batched rendering.** One React state update per event would make the UI
 *    slower than the engine it is watching. Incoming events are staged in a ref
 *    and flushed on an interval.
 * 3. **Pausing without disconnecting.** A paused stream keeps its socket and
 *    drops what arrives, so resuming does not replay a backlog.
 *
 * Where the events *come from* is the transport's business. Reconnection with
 * backoff used to live here, and moved out with it: a transport running the
 * engine inside the tab cannot disconnect, so there would be nothing for that
 * code to do.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { getTransport, type ConnectionState } from "@/lib/transport";
import type { TraceRecordModel } from "@/types/api";

export type { ConnectionState };

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
    return getTransport().subscribe(databaseId, {
      onState: setConnection,
      onEvents: (batch) => {
        if (pausedRef.current) return;
        pending.current.push(...batch);
      },
      onDropped: (count) => setDroppedByServer((total) => total + count),
    });
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
