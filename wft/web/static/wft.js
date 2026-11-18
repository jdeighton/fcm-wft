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

  async function check() {
    try {
      const r = await fetch("/status", { cache: "no-store" });
      if (!r.ok) return;
      const s = await r.json();
      if (s.version === seen.version && s.read_only === seen.read_only && s.conflicts === seen.conflicts) return;
      if (dirty) document.getElementById("updates").hidden = false;
      else location.reload();
    } catch (_) { /* backend restarting; try again next time */ }
  }
  setInterval(check, 5000);
})();
