// Poll one request chain at a time; hidden pages stop reading until visible again.
export function pollWhileVisible(refresh: (signal: AbortSignal) => Promise<void>, interval = 5000) {
  const controller = new AbortController();
  let stopped = false;
  let running = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  async function run() {
    if (stopped || running || document.hidden) return;
    running = true;
    try { await refresh(controller.signal); }
    finally {
      running = false;
      if (!stopped && !document.hidden) timer = setTimeout(run, interval);
    }
  }
  function visible() {
    clearTimeout(timer);
    if (!document.hidden) void run();
  }
  document.addEventListener('visibilitychange', visible);
  void run();
  return () => {
    stopped = true;
    controller.abort();
    clearTimeout(timer);
    document.removeEventListener('visibilitychange', visible);
  };
}
