// Operator panel: live status over SSE, confirmations, double-submit guard, countdown,
// copy-to-clipboard for addresses and the light/dark theme switch.
(function () {
  "use strict";

  var UNIVERSE_TONES = { FRESH: "good", STALE: "warn", NOT_SYNCED: "warn" };
  var MODE_TONES = { SHADOW: "info", APPROVAL: "warn", LIVE: "bad" };

  function setText(name, value) {
    document.querySelectorAll('[data-pulse="' + name + '"]').forEach(function (node) {
      node.textContent = value;
    });
  }

  // Recolours the pill around a live value: "pill good", "pill bad", ...
  function setTone(name, tone) {
    document.querySelectorAll('[data-pulse="' + name + '"]').forEach(function (node) {
      var pill = node.closest(".pill");
      if (pill) {
        pill.className = "pill" + (tone ? " " + tone : "");
      }
    });
  }

  // The page's one stream (data-stream): over HTTP/1.1 a browser keeps six connections
  // per host, so every live part of a page listens on this single EventSource.
  function connectStream() {
    var bar = document.querySelector("[data-stream]");
    if (!bar || !window.EventSource) {
      return;
    }
    var source = new EventSource(bar.getAttribute("data-stream"));
    listenStatus(source);
    liveOverview(source);
  }

  function listenStatus(source) {
    source.addEventListener("status", function (event) {
      var pulse;
      try {
        pulse = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      setText("mode", pulse.mode.mode);
      setTone("mode", MODE_TONES[pulse.mode.mode] || "");
      setText("kill", pulse.mode.kill_switch_active ? "активен" : "выключен");
      setTone("kill", pulse.mode.kill_switch_active ? "bad" : "good");
      setText("alerts", String(pulse.open_alerts));
      setTone("alerts", pulse.open_alerts > 0 ? "bad" : "");
      setText("approvals", String(pulse.approvals_pending));
      setTone("approvals", pulse.approvals_pending > 0 ? "warn" : "");
      setText("universe", pulse.universe_state);
      setTone("universe", UNIVERSE_TONES[pulse.universe_state] || "");
      setText("time", pulse.generated_at.replace("T", " ").replace(/(\.\d+)?(Z|[+-]\d\d:\d\d)$/, "") + " UTC");
    });
  }

  function guardForms() {
    document.querySelectorAll("form").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var question = form.getAttribute("data-confirm");
        if (question && !window.confirm(question)) {
          event.preventDefault();
          return;
        }
        form.querySelectorAll('button[type="submit"]').forEach(function (button) {
          button.disabled = true;
        });
      });
    });
  }

  function countdown() {
    var node = document.querySelector("[data-countdown]");
    if (!node) {
      return;
    }
    var left = parseInt(node.getAttribute("data-countdown"), 10) || 0;
    var timer = window.setInterval(function () {
      left = Math.max(left - 1, 0);
      node.textContent = String(left);
      if (left === 0) {
        window.clearInterval(timer);
        document.querySelectorAll("[data-approve]").forEach(function (button) {
          button.disabled = true;
        });
        node.textContent = "0 — обновите страницу для новой симуляции";
      }
    }, 1000);
  }

  function copyButtons() {
    document.querySelectorAll("[data-copy]").forEach(function (button) {
      button.addEventListener("click", function () {
        if (!navigator.clipboard) {
          return;
        }
        navigator.clipboard.writeText(button.getAttribute("data-copy")).then(function () {
          button.classList.add("copied");
          window.setTimeout(function () {
            button.classList.remove("copied");
          }, 1200);
        });
      });
    });
  }

  function themeToggle() {
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var root = document.documentElement;
        var light = root.getAttribute("data-theme") !== "light";
        if (light) {
          root.setAttribute("data-theme", "light");
        } else {
          root.removeAttribute("data-theme");
        }
        try {
          window.localStorage.setItem("mvb-theme", light ? "light" : "dark");
        } catch (error) {
          // The choice lasts until the page is reloaded.
        }
      });
    });
  }

  // The overview's live parts: each "overview" event carries server-rendered HTML for
  // every [data-live="<part>"]; only changed parts are swapped, the page never reloads.
  function liveOverview(source) {
    if (!document.querySelector("[data-live]")) {
      return;
    }
    var note = document.querySelector("[data-live-note]");
    var shown = {};
    source.addEventListener("overview", function (event) {
      var parts;
      try {
        parts = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      Object.keys(parts).forEach(function (name) {
        var node = document.querySelector('[data-live="' + name + '"]');
        if (node && shown[name] !== parts[name]) {
          node.innerHTML = parts[name];
          shown[name] = parts[name];
        }
      });
      if (note) {
        note.className = "pill bare live";
        // The panel shows time in UTC everywhere.
        note.textContent = "обновлено " + new Date().toISOString().slice(11, 19) + " UTC";
      }
    });
    source.addEventListener("error", function () {
      if (note) {
        note.className = "pill warn";
        note.textContent = "нет связи — переподключение";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    connectStream();
    guardForms();
    countdown();
    copyButtons();
    themeToggle();
    liveOverview();
  });
})();
