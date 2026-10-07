(function () {
  "use strict";

  // Balance lines are fetched after render so one slow provider API cannot
  // block the routes page; a route whose key no longer answers shows nothing.
  var cells = document.querySelectorAll("[data-balance-url]");
  Array.prototype.forEach.call(cells, function (cell) {
    fetch(cell.getAttribute("data-balance-url"), { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json().catch(function () { return {}; });
      })
      .then(function (body) {
        if (body && body.ok && body.text) {
          cell.textContent = body.text;
          cell.classList.add("has-balance");
        }
      })
      .catch(function () {});
  });
})();
