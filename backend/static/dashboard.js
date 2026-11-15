"use strict";
let timer,
  loading = false;
const busySessions = new Set();
async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const { sessions } = await UI.request("/challenges");
    document.getElementById("error").hidden = true;
    const stats = document.getElementById("stats");
    stats.replaceChildren();
    for (const state of ["running", "completed", "pending", "failed"]) {
      const card = UI.element("div", null, "stat " + state);
      card.append(
        UI.element("strong", sessions.filter((s) => s.status === state).length),
        UI.element("span", state),
      );
      stats.append(card);
    }
    const list = document.getElementById("sessions");
    list.replaceChildren();
    if (!sessions.length) {
      const empty = UI.element("div", null, "empty");
      empty.append(
        UI.element("h3", "A fresh start"),
        UI.element("p", "Launch a challenge to see your sessions here."),
      );
      list.append(empty);
    }
    for (const session of sessions) {
      const row = UI.element("article", null, "session-row");
      const status = UI.element("div");
      status.append(UI.badge(session.status));
      const name = UI.element("div");
      const link = UI.element(
        "a",
        session.challenge_name || "Untitled challenge",
      );
      link.href = "/?session=" + encodeURIComponent(session.session_id);
      name.append(
        link,
        UI.element("small", session.model),
        UI.element("small", new Date(session.created_at).toLocaleString()),
      );
      const target = UI.element(
        "div",
        session.challenge_url ||
          (session.host ? session.host + ":" + session.port : "Handout files"),
      );
      target.append(
        UI.element("small", session.challenge_type || "Auto-detect"),
      );
      const result = UI.element("div");
      result.append(
        UI.element(
          "code",
          session.found_flag || session.error_message || "Awaiting result",
        ),
        UI.element(
          "small",
          "Estimated cost: " + UI.cost(session.estimated_cost_usd),
        ),
      );
      const buttons = UI.element("div", null, "button-row");
      function button(label, fn) {
        const b = UI.element("button", label);
        b.disabled = busySessions.has(session.session_id);
        b.onclick = () =>
          UI.busy(b, async () => {
            busySessions.add(session.session_id);
            try {
              await fn();
            } finally {
              busySessions.delete(session.session_id);
              await refresh();
            }
          });
        buttons.append(b);
      }
      if (["pending", "running"].includes(session.status))
        button("Stop", () =>
          UI.request(`/challenges/${session.session_id}/stop`, {
            method: "POST",
          }),
        );
      if (session.found_flag)
        button("Copy flag", () => UI.copy(session.found_flag));
      if (session.status === "completed")
        button("Save write-up", async () => {
          UI.toast("Generating write-up…");
          const r = await UI.request(`/challenges/${session.session_id}/save`, {
            method: "POST",
          });
          UI.toast("Saved to " + r.directory);
        });
      button("Delete", async () => {
        if (confirm("Delete this session and its action history?"))
          await UI.request(`/challenges/${session.session_id}`, {
            method: "DELETE",
          });
      });
      row.append(status, name, target, result, buttons);
      list.append(row);
    }
    document.getElementById("refresh-status").textContent =
      "Updated " + new Date().toLocaleTimeString();
  } catch (e) {
    UI.error(e);
  } finally {
    loading = false;
  }
}
document.getElementById("refresh").onclick = refresh;
document.getElementById("flag-settings").onsubmit = (e) => {
  e.preventDefault();
  const button = e.target.querySelector("button");
  UI.busy(button, async () => {
    await UI.request("/settings/flag-format", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        flag_format: e.target.elements.flag_format.value,
      }),
    });
    UI.toast("Default saved");
  });
};
refresh();
timer = setInterval(refresh, 5000);
window.addEventListener("pagehide", () => clearInterval(timer));
