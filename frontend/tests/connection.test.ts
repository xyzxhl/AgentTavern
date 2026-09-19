import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getSession: vi.fn() }));

vi.mock("../src/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api")>();
  return { ...actual, api: { ...actual.api, getSession: mocks.getSession } };
});

import { useSession } from "../src/store";

type Deferred = {
  promise: Promise<unknown>;
  resolve: (value: unknown) => void;
};

function deferred(): Deferred {
  let resolve!: (value: unknown) => void;
  const promise = new Promise<unknown>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  static readonly OPEN = 1;

  readyState = FakeWebSocket.OPEN;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  sent: string[] = [];

  constructor(readonly url: string) {
    sockets.push(this);
  }

  send(message: string): void {
    this.sent.push(message);
  }

  close(): void {
    this.readyState = 3;
    this.onclose?.({ wasClean: true } as CloseEvent);
  }
}

function detail(id: string): unknown {
  return {
    session: { id, meta: {} },
    path: [],
    state: {},
    siblings: {},
  };
}

afterEach(() => {
  useSession.getState().disconnect();
  mocks.getSession.mockReset();
  sockets.length = 0;
  vi.unstubAllGlobals();
});

describe("session connection lifecycle", () => {
  it("test_only_the_latest_connection_attempt_installs_a_socket", async () => {
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    const first = deferred();
    const second = deferred();
    mocks.getSession.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    useSession.setState({
      resumable: true,
      usage: { tokensIn: 12, tokensOut: 34 },
    });

    const stale = useSession.getState().connect("session-a");
    const current = useSession.getState().connect("session-b");

    first.resolve(detail("session-a"));
    await stale;
    second.resolve(detail("session-b"));
    await current;

    expect(sockets).toHaveLength(1);
    expect(sockets[0]?.url).toContain("/sessions/session-b/ws");
    expect(useSession.getState().sessionId).toBe("session-b");
    expect(useSession.getState().notice).toBeNull();
    expect(useSession.getState().resumable).toBe(false);
    expect(useSession.getState().usage).toEqual({ tokensIn: 0, tokensOut: 0 });
  });

  it("hides an answered question as soon as the answer is sent", async () => {
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mocks.getSession.mockResolvedValue(detail("session-question"));
    await useSession.getState().connect("session-question");

    sockets[0]?.onmessage?.({
      data: JSON.stringify({
        type: "question",
        question_id: "question-1",
        question: "选哪条路？",
        options: ["左", "右", "原地等待"],
      }),
    } as MessageEvent);
    expect(useSession.getState().phase).toBe("asking");

    useSession.getState().answerQuestion("question-1", "右");

    expect(useSession.getState().question).toBeNull();
    expect(useSession.getState().phase).toBe("running");
    expect(sockets[0]?.sent).toEqual([
      JSON.stringify({ type: "answer", question_id: "question-1", selected: "右" }),
    ]);
  });
});
