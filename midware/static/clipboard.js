/* One clipboard helper for every page: navigator.clipboard where it exists
 * (secure contexts only), a hidden-textarea fallback where it does not. A
 * self-hosted box is often reached over plain HTTP, where the modern API is
 * simply absent rather than failing, so both paths are load-bearing. */
(function () {
  "use strict";

  function legacyCopy(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "true");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }
    document.body.removeChild(area);
    return ok;
  }

  window.mwCopy = function (text) {
    if (!text) {
      return Promise.resolve(false);
    }
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).then(
        function () {
          return true;
        },
        function () {
          return legacyCopy(text);
        }
      );
    }
    return Promise.resolve(legacyCopy(text));
  };

  window.mwFlash = function (button, text) {
    if (!button) {
      return;
    }
    var original = button.textContent;
    button.textContent = text;
    button.disabled = true;
    window.setTimeout(function () {
      button.textContent = original;
      button.disabled = false;
    }, 1100);
  };
})();
