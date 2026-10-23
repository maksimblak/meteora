// Operator panel: live status over SSE, confirmations, double-submit guard, countdown.
(function () {
  "use strict";

  function setText(name, value) {
    document.querySelectorAll('[data-pulse="' + name + '"]').forEach(function (node) {
      node.textContent = value;
    });
  }

  function connectStatus() {
    var bar = document.querySelector("[data-stream]");
    if (!bar || !window.EventSource) {
      return;
    }
    var source = new EventSource(bar.getAttribute("data-stream"));
    source.addEventListener("status", function (event) {
      var pulse;
      try {
        pulse = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      setText("mode", pulse.mode.mode);
      setText("kill", pulse.mode.kill_switch_active ? "активен" : "выключен");
      document.querySelectorAll('[data-pulse="kill"]').forEach(function (node) {
        node.className = pulse.mode.kill_switch_active ? "bad" : "ok";
      });
      setText("alerts", String(pulse.open_alerts));
      setText("approvals", String(pulse.approvals_pending));
      setText("universe", pulse.universe_state);
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

  document.addEventListener("DOMContentLoaded", function () {
    connectStatus();
    guardForms();
    countdown();
  });
})();
