import "@testing-library/jest-dom/vitest";

// jsdom has no WebSocket. The event-stream hook only needs the constructor to
// exist and to never throw; the stream itself is tested against the real
// server in tests/integration/test_websocket.py.
class MockWebSocket {
  static readonly OPEN = 1;
  readyState = MockWebSocket.OPEN;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send(): void {}
  close(): void {}
}

Object.defineProperty(globalThis, "WebSocket", {
  writable: true,
  value: MockWebSocket,
});
