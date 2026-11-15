"use strict";
window.UI = {
  async request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok)
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail || "Request failed"),
      );
    return data;
  },
  element(tag, text, cls) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  },
  error(error) {
    const el = document.getElementById("error");
    el.textContent = error.message || String(error);
    el.hidden = false;
  },
  toast(text) {
    const el = document.getElementById("toast");
    el.textContent = text;
    el.hidden = false;
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => (el.hidden = true), 3500);
  },
  badge(status) {
    return this.element("span", status, "badge " + status);
  },
  cost(value) {
    return typeof value === "number" ? "$" + value.toFixed(6) : "Unavailable";
  },
  async copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.toast("Copied to clipboard");
    } catch (e) {
      this.error(e);
    }
  },
  async busy(button, action) {
    button.disabled = true;
    try {
      await action();
    } catch (e) {
      this.error(e);
    } finally {
      button.disabled = false;
    }
  },
};
const themeSelect = document.getElementById("theme");
try {
  themeSelect.value = localStorage.getItem("theme") || "system";
} catch (e) {}
function applyTheme() {
  const theme = themeSelect.value;
  document.documentElement.dataset.theme =
    theme === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : theme;
}
themeSelect.addEventListener("change", () => {
  try {
    localStorage.setItem("theme", themeSelect.value);
  } catch (e) {}
  applyTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener(
  "change",
  applyTheme,
);
applyTheme();
