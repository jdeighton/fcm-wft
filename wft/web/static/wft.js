// Notice other users' changes (and read-only mode) by polling /status.
// Reload automatically unless the user has started typing, in which case offer a reload link.
(() => {
  const body = document.body;
  if (body.dataset.autorefresh !== "true") return;
  const seen = {
    version: Number(body.dataset.version),
    read_only: body.dataset.readOnly === "true",
    conflicts: Number(body.dataset.conflicts),
  };
  let dirty = false;
  document.addEventListener("input", () => { dirty = true; });

  // Unread count in the tab title, and (if chosen) the browser's own desktop notifications.
  let lastNotice = Number(body.dataset.lastNotice || 0);
  const useBrowser = body.dataset.browserNotifications === "true" && "Notification" in window;
  const baseTitle = document.title.replace(/^\(\d+\) /, "");
  function showUnread(n) { document.title = (n ? `(${n}) ` : "") + baseTitle; }
  const enable = document.getElementById("enable-notifications");
  if (enable && useBrowser && Notification.permission === "default") {
    enable.hidden = false;
    enable.addEventListener("click", () => Notification.requestPermission().then(() => { enable.hidden = true; }));
  }

  async function check() {
    try {
      const r = await fetch(`/status?after=${lastNotice}`, { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      showUnread(s.unread);
      for (const n of s.notices || []) {
        lastNotice = Math.max(lastNotice, n.id);
        if (useBrowser && Notification.permission === "granted") {
          const note = new Notification(n.title, { body: n.text, tag: `wft-${n.id}` });
          note.onclick = () => { window.focus(); location.href = n.url; };
        }
      }
      if (s.version === seen.version && s.read_only === seen.read_only && s.conflicts === seen.conflicts) return;
      if (dirty) document.getElementById("updates").hidden = false;
      else location.reload();
    } catch (_) { /* backend restarting; try again next time */ }
  }
  setInterval(check, 5000);
})();
