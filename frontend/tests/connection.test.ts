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
  onopen: (() => void) | null = null;
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

function detail(id: string, leaf: string | null = null): unknown {
  return {
    session: { id, meta: {}, active_leaf_id: leaf, editable_node_id: leaf },
    path: leaf ? [{ id: leaf }] : [],
    state: {},
    siblings: {},
  };
}

afterEach(() => {
  useSession.getState().disconnect();
  mocks.getSession.mockReset();
  sockets.length = 0;
  vi.unstubAllGlobals();
  vi.useRealTimers();
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

  it("reconnects_an_unexpectedly_closed_session_without_user_refresh", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mocks.getSession
      .mockResolvedValueOnce(detail("session-reconnect"))
      .mockResolvedValueOnce(detail("session-reconnect", "run-finished-offline"));
    await useSession.getState().connect("session-reconnect");
    sockets[0]?.onmessage?.({
      data: JSON.stringify({
        type: "run_started",
        run_id: "run-finished-offline",
        input: "继续远征",
      }),
    } as MessageEvent);
    expect(useSession.getState().phase).toBe("running");

    sockets[0]?.onclose?.({ wasClean: false, code: 1006 } as CloseEvent);
    expect(useSession.getState().notice).toBe("连接断开，正在重连…");

    await vi.advanceTimersByTimeAsync(1_000);
    expect(sockets).toHaveLength(2);
    expect(sockets[1]?.url).toContain("/sessions/session-reconnect/ws");

    sockets[1]?.onopen?.();
    await vi.waitFor(() => expect(mocks.getSession).toHaveBeenCalledTimes(2));
    expect(useSession.getState().notice).toBeNull();
    expect(useSession.getState().phase).toBe("idle");
    expect(useSession.getState().live).toBeNull();
    expect(useSession.getState().path.at(-1)?.id).toBe("run-finished-offline");
  });

  it("does_not_fight_a_new_page_that_takes_over_the_session", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mocks.getSession.mockResolvedValue(detail("session-taken-over"));
    await useSession.getState().connect("session-taken-over");

    sockets[0]?.onclose?.({ wasClean: true, code: 4012 } as CloseEvent);
    await vi.advanceTimersByTimeAsync(10_000);

    expect(sockets).toHaveLength(1);
    expect(useSession.getState().notice).toBe("会话已由新的页面接管");
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

  it("removes a pending question when its run is interrupted", async () => {
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mocks.getSession.mockResolvedValue(detail("session-interrupted-question"));
    await useSession.getState().connect("session-interrupted-question");

    sockets[0]?.onmessage?.({
      data: JSON.stringify({
        type: "question",
        question_id: "question-1",
        question: "选哪条路？",
        options: ["左", "右", "原地等待"],
      }),
    } as MessageEvent);
    expect(useSession.getState().question?.question_id).toBe("question-1");

    sockets[0]?.onmessage?.({
      data: JSON.stringify({ type: "run_interrupted", resumable: true }),
    } as MessageEvent);

    expect(useSession.getState().phase).toBe("idle");
    expect(useSession.getState().question).toBeNull();
    expect(useSession.getState().resumable).toBe(true);
  });

  it("removes a pending question when its run fails", async () => {
    vi.stubGlobal("location", { protocol: "http:", host: "127.0.0.1:5173" });
    vi.stubGlobal("WebSocket", FakeWebSocket);
    mocks.getSession.mockResolvedValue(detail("session-failed-question"));
    await useSession.getState().connect("session-failed-question");

    sockets[0]?.onmessage?.({
      data: JSON.stringify({
        type: "run_started",
        run_id: "run-question",
        input: "先问我",
      }),
    } as MessageEvent);
    sockets[0]?.onmessage?.({
      data: JSON.stringify({
        type: "question",
        question_id: "question-1",
        question: "选哪条路？",
        options: ["左", "右", "原地等待"],
      }),
    } as MessageEvent);
    sockets[0]?.onmessage?.({
      data: JSON.stringify({ type: "run_failed", reason: "测试失败" }),
    } as MessageEvent);

    expect(useSession.getState().phase).toBe("idle");
    expect(useSession.getState().question).toBeNull();
    expect(useSession.getState().failed).toEqual({ input: "先问我", reason: "测试失败" });
  });
});
