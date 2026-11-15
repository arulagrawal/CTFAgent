"use strict";
const $ = (id) => document.getElementById(id),
  form = $("challenge-form");
let catalog,
  session,
  socket,
  pollTimer,
  reconnectTimer,
  generation = 0,
  follow = true;
const actions = new Map();
function active() {
  return session && ["pending", "running"].includes(session.status);
}
function targetChanged() {
  const type = $("target_type").value;
  $("url-block").hidden = type !== "url";
  $("socket-block").hidden = type !== "socket";
  form.elements.url.disabled = type !== "url";
  form.elements.url.required = type === "url";
  for (const key of ["host", "port"]) {
    form.elements[key].disabled = type !== "socket";
    form.elements[key].required = type === "socket";
  }
}
$("target_type").onchange = targetChanged;
targetChanged();
function modelChanged() {
  const custom = $("model").value === "custom";
  $("custom-model-block").hidden = !custom;
  $("custom-model").required = custom;
  const info = catalog.models.find((m) => m.api_string === $("model").value);
  $("reasoning_effort").replaceChildren(new Option("Model default", ""));
  for (const effort of info?.reasoning_efforts || [
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
  ])
    $("reasoning_effort").add(new Option(effort, effort));
}
$("model").onchange = modelChanged;
async function init() {
  try {
    catalog = await UI.request("/models");
    $("model").replaceChildren(
      ...catalog.models.map((m) => new Option(m.name, m.api_string)),
      new Option("Custom model…", "custom"),
    );
    if (catalog.models.some((m) => m.api_string === catalog.default_model))
      $("model").value = catalog.default_model;
    else {
      $("model").value = "custom";
      $("custom-model").value = catalog.default_model;
    }
    modelChanged();
    const health = await UI.request("/health");
    $("health").textContent = health.ai_agent_ready
      ? "Backend ready"
      : "Backend needs configuration";
    $("health").className =
      "badge " + (health.ai_agent_ready ? "completed" : "pending");
    $("start-btn").disabled = !health.ai_agent_ready;
    const id = new URLSearchParams(location.search).get("session");
    if (id) await load(id);
  } catch (e) {
    UI.error(e);
  }
}
function disconnect() {
  generation++;
  clearTimeout(pollTimer);
  clearTimeout(reconnectTimer);
  if (socket) {
    socket.onclose = null;
    socket.close();
    socket = null;
  }
}
async function load(id) {
  disconnect();
  actions.clear();
  $("action-log").replaceChildren();
  session = await UI.request("/challenges/" + encodeURIComponent(id));
  renderSession();
  for (const action of session.actions || []) renderAction(action);
  history.replaceState(null, "", "/?session=" + encodeURIComponent(id));
  if (active()) {
    connect(generation);
    poll(generation);
  } else $("connection").textContent = "Session finished";
}
function renderSession() {
  if (!session) return;
  $("session-panel").hidden = false;
  $("session-status").textContent = session.status;
  $("session-status").className = "badge " + session.status;
  $("session-details").replaceChildren();
  const end = active() ? Date.now() : new Date(session.updated_at).getTime();
  for (const [label, value] of Object.entries({
    Challenge: session.challenge_name || "Untitled challenge",
    Model: session.model,
    Reasoning: session.reasoning_effort || "Model default",
    Target:
      session.challenge_url ||
      (session.host ? session.host + ":" + session.port : "Handout files"),
    Duration:
      Math.max(0, Math.floor((end - new Date(session.created_at)) / 1000)) +
      " seconds",
    "Estimated cost": UI.cost(session.estimated_cost_usd),
    "Session ID": session.session_id,
  })) {
    $("session-details").append(
      UI.element("dt", label),
      UI.element("dd", value),
    );
  }
  $("session-error").textContent = session.error_message || "";
  $("flag-result").hidden = !session.found_flag;
  $("flag-value").textContent = session.found_flag || "";
  $("stop-btn").hidden = !active();
  $("save-btn").hidden = session.status !== "completed";
  $("upload-btn").disabled = !active();
  $("uploaded-files").replaceChildren(
    ...(session.uploaded_files || []).map((name) => UI.element("li", name)),
  );
  if (!active()) {
    $("thinking").hidden = true;
  }
}
function renderAction(action) {
  const key = action.id || action.timestamp + "|" + action.action_type;
  let entry = actions.get(key);
  if (!entry) {
    if (!actions.size) $("action-log").replaceChildren();
    const card = UI.element("article", null, "action"),
      head = UI.element("div", null, "action-header"),
      reason = UI.element("p"),
      command = UI.element("pre", null, "command"),
      output = UI.element("pre");
    head.append(
      UI.element("strong", action.action_type.replaceAll("_", " ")),
      UI.element("time", new Date(action.timestamp).toLocaleTimeString()),
    );
    card.append(head, reason, command, output);
    $("action-log").append(card);
    entry = { card, reason, command, output };
    actions.set(key, entry);
  }
  entry.reason.textContent = action.reasoning || "";
  entry.reason.hidden = !action.reasoning;
  entry.command.textContent = action.command || "";
  entry.command.hidden = !action.command;
  if (action.output !== undefined)
    entry.output.textContent = action.output || "";
  if (follow) $("action-log").scrollTop = $("action-log").scrollHeight;
}
function connect(token) {
  if (token !== generation || !active()) return;
  socket = new WebSocket(
    `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${session.session_id}`,
  );
  socket.onopen = () => {
    $("connection").textContent = "Live";
  };
  socket.onmessage = (e) => {
    if (token !== generation) return;
    try {
      const msg = JSON.parse(e.data),
        d = msg.data;
      if (["action", "action_create", "flag_found"].includes(msg.type))
        renderAction(d);
      else if (msg.type === "action_chunk") {
        const entry = actions.get(d.id);
        if (entry) {
          entry.output.append(document.createTextNode(d.chunk || ""));
          if (follow) $("action-log").scrollTop = $("action-log").scrollHeight;
        }
      } else if (msg.type === "action_complete") {
        const entry = actions.get(d.id);
        if (entry && d.final_output !== undefined)
          entry.output.textContent = d.final_output;
      } else if (msg.type === "ai_status") $("thinking").hidden = !d.thinking;
      else if (msg.type === "status") {
        session = { ...session, ...d };
        renderSession();
      } else if (msg.type === "error") UI.error(d.error);
    } catch (e) {
      console.warn("Invalid live update", e);
    }
  };
  socket.onclose = () => {
    if (token === generation && active()) {
      $("connection").textContent = "Reconnecting · polling";
      reconnectTimer = setTimeout(() => connect(token), 3000);
    }
  };
}
async function poll(token) {
  if (token !== generation) return;
  try {
    const latest = await UI.request("/challenges/" + session.session_id);
    if (token !== generation) return;
    session = latest;
    renderSession();
    for (const action of latest.actions || []) renderAction(action);
    if (!active()) {
      disconnect();
      $("connection").textContent = "Session finished";
      return;
    }
  } catch (e) {
    $("connection").textContent = "Connection interrupted";
  }
  if (token === generation) pollTimer = setTimeout(() => poll(token), 3000);
}
form.onsubmit = (e) => {
  e.preventDefault();
  UI.busy($("start-btn"), async () => {
    const data = new FormData(form);
    if ($("model").value === "custom")
      data.set("model", $("custom-model").value.trim());
    for (const [key, value] of [...data])
      if (value === "" || (value instanceof File && !value.name))
        data.delete(key);
    $("error").hidden = true;
    const result = await UI.request("/challenges/start", {
      method: "POST",
      body: data,
    });
    await load(result.session_id);
  });
};
$("stop-btn").onclick = () =>
  UI.busy($("stop-btn"), async () => {
    await UI.request(`/challenges/${session.session_id}/stop`, {
      method: "POST",
    });
    await load(session.session_id);
  });
$("delete-btn").onclick = () => {
  if (confirm("Delete this session and its action history?"))
    UI.busy($("delete-btn"), async () => {
      await UI.request("/challenges/" + session.session_id, {
        method: "DELETE",
      });
      disconnect();
      location.href = "/";
    });
};
$("save-btn").onclick = () =>
  UI.busy($("save-btn"), async () => {
    UI.toast("Generating write-up…");
    const result = await UI.request(`/challenges/${session.session_id}/save`, {
      method: "POST",
    });
    UI.toast("Saved to " + result.directory);
  });
$("copy-flag").onclick = () => UI.copy(session.found_flag);
$("upload-btn").onclick = () =>
  UI.busy($("upload-btn"), async () => {
    for (const file of $("extra-files").files) {
      const data = new FormData();
      data.set("file", file);
      await UI.request(`/challenges/${session.session_id}/upload`, {
        method: "POST",
        body: data,
      });
    }
    $("extra-files").value = "";
    UI.toast("Files uploaded");
  });
$("action-log").onscroll = () => {
  const el = $("action-log");
  follow = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  $("follow-btn").hidden = follow;
};
$("follow-btn").onclick = () => {
  follow = true;
  $("action-log").scrollTop = $("action-log").scrollHeight;
};
window.addEventListener("pagehide", disconnect);
init();
