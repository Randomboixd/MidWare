/* Masked credentials, shared by the keys list and the key edit page: one
 * delegated pair of listeners drives every .mw-token on the page, so a
 * template only ships the markup. Reveal is a toggle between the masked
 * text and the whole key; Copy answers with a toast through clipboard.js,
 * because a copy control must always answer. */
(function () {
  "use strict";

  document.addEventListener("click", function (event) {
    var copy = event.target.closest("[data-copy-token]");
    if (copy) {
      window.mwCopy(copy.getAttribute("data-copy-token")).then(function (ok) {
        window.mwFlash(copy, ok ? "Copied" : "Copy failed");
      });
      return;
    }
    var value = event.target.closest("[data-reveal-token]");
    if (value) {
      var full = value.getAttribute("data-reveal-token");
      value.textContent = value.textContent === full ? value.getAttribute("data-masked") : full;
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    var value = event.target.closest("[data-reveal-token]");
    if (!value) {
      return;
    }
    event.preventDefault();
    value.click();
  });
})();
