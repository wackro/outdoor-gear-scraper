// Client-side sort + filter for the deals grid. No build step, no dependencies.
(function () {
  var grid = document.getElementById("deals");
  if (!grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll(".card"));
  var sortSel = document.getElementById("sort");
  var brandSel = document.getElementById("brand");
  var categorySel = document.getElementById("category");
  var countEl = document.getElementById("visible-count");
  var emptyEl = document.getElementById("empty-state");

  var num = function (card, attr) { return parseFloat(card.getAttribute(attr)) || 0; };
  var str = function (card, attr) { return card.getAttribute(attr) || ""; };

  var sorters = {
    discount: function (a, b) { return num(b, "data-discount") - num(a, "data-discount"); },
    score: function (a, b) { return num(b, "data-score") - num(a, "data-score"); },
    recent: function (a, b) { return str(b, "data-first-seen").localeCompare(str(a, "data-first-seen")); },
    "price-asc": function (a, b) { return num(a, "data-price") - num(b, "data-price"); },
    "price-desc": function (a, b) { return num(b, "data-price") - num(a, "data-price"); }
  };

  function apply() {
    var brand = brandSel.value;
    var category = categorySel.value;
    var sorter = sorters[sortSel.value] || sorters.discount;

    cards.sort(sorter);

    var visible = 0;
    cards.forEach(function (card) {
      var show = (!brand || str(card, "data-brand") === brand) &&
                 (!category || str(card, "data-category") === category);
      card.hidden = !show;
      if (show) visible++;
      grid.appendChild(card); // re-append in sorted order
    });

    if (countEl) countEl.textContent = visible;
    if (emptyEl) emptyEl.hidden = visible !== 0;
  }

  [sortSel, brandSel, categorySel].forEach(function (el) {
    if (el) el.addEventListener("change", apply);
  });
  apply();
})();
