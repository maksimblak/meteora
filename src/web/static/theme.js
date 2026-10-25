// Applies the saved light/dark choice before the page paints (no flash of the wrong theme).
(function () {
  "use strict";
  try {
    if (window.localStorage.getItem("mvb-theme") === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    }
  } catch (error) {
    // Storage may be blocked: the default dark theme stays.
  }
})();
