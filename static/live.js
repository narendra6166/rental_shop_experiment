(() => {
  const live = document.getElementById("live-updates");
  if (!live) return;

  const message = live.querySelector("[data-live-message]");
  const reload = live.querySelector("button");
  let dirty = false;
  let submitting = false;
  let pending = false;
  let timer;
  let checking = false;

  const show = (text, refresh) => {
    live.hidden = false;
    message.textContent = text;
    reload.hidden = !refresh;
  };
  const editing = () => document.activeElement?.matches(
    "input, select, textarea, button, [contenteditable='true']"
  );
  const markDirty = (event) => {
    if (!live.contains(event.target)) dirty = true;
  };
  document.addEventListener("input", markDirty);
  document.addEventListener("change", markDirty);
  document.addEventListener("submit", () => { submitting = true; });
  reload.addEventListener("click", () => {
    if (!dirty || window.confirm(live.dataset.confirm)) window.location.reload();
  });

  const schedule = (delay = 5000) => {
    clearTimeout(timer);
    timer = setTimeout(check, delay);
  };
  async function check() {
    if (checking) return;
    if (document.hidden || submitting) {
      schedule();
      return;
    }
    checking = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(live.dataset.url, {
        cache: "no-store", credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/json" }
      });
      if (response.redirected || response.status === 401 || response.status === 403) {
        show(live.dataset.session, true);
        return;
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Live updates unavailable");
      }
      const data = await response.json();
      if (typeof data.revision !== "string") throw new Error("Invalid live revision");
      pending = pending || data.revision !== live.dataset.revision;
      if (pending) {
        if (!dirty && !editing()) {
          window.location.reload();
          return;
        }
        show(live.dataset.changed, true);
      } else {
        live.hidden = true;
      }
    } catch (error) {
      show(live.dataset.offline, false);
    } finally {
      clearTimeout(timeout);
      checking = false;
      schedule();
    }
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) schedule(0);
  });
  window.addEventListener("online", () => schedule(0));
  window.addEventListener("offline", () => show(live.dataset.offline, false));
  window.addEventListener("pageshow", () => { submitting = false; });
  schedule();
})();
