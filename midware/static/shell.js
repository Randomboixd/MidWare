/* The phone shell: the More sheet under the tab bar.
 *
 * The bar itself needs no script — every tab is a link, and the sheet is the
 * only thing that opens. Closing on Escape and on a tap outside is what makes
 * it feel like a sheet rather than a stuck menu. */
(function () {
  "use strict";

  var button = document.getElementById("mw-more");
  var panel = document.getElementById("mw-more-panel");
  if (!button || !panel) {
    return;
  }

  function setOpen(open) {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.classList.toggle("is-open", open);
    panel.hidden = !open;
  }

  button.addEventListener("click", function (event) {
    event.stopPropagation();
    setOpen(panel.hidden);
  });

  document.addEventListener("click", function (event) {
    if (panel.hidden) {
      return;
    }
    if (!panel.contains(event.target) && event.target !== button) {
      setOpen(false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !panel.hidden) {
      setOpen(false);
      button.focus();
    }
  });

  // A tap on a link inside the sheet navigates anyway; closing first avoids a
  // panel left open behind a bfcache restore.
  Array.prototype.forEach.call(panel.querySelectorAll("a"), function (link) {
    link.addEventListener("click", function () {
      setOpen(false);
    });
  });
})();
