// Client-side gender tabs + sort/filter for the deals grid. No dependencies.
(function () {
  var grid = document.getElementById("deals");
  if (!grid) return;

  var cards = Array.prototype.slice.call(grid.querySelectorAll(".card"));
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var sortSel = document.getElementById("sort");
  var brandSel = document.getElementById("brand");
  var typeSel = document.getElementById("type");
  var countEl = document.getElementById("visible-count");
  var emptyEl = document.getElementById("empty-state");

  var gender = "men"; // men is the default section

  var num = function (c, a) { return parseFloat(c.getAttribute(a)) || 0; };
  var str = function (c, a) { return c.getAttribute(a) || ""; };

  var sorters = {
    discount: function (a, b) { return num(b, "data-discount") - num(a, "data-discount"); },
    score: function (a, b) { return num(b, "data-score") - num(a, "data-score"); },
    recent: function (a, b) { return str(b, "data-first-seen").localeCompare(str(a, "data-first-seen")); },
    "price-asc": function (a, b) { return num(a, "data-price") - num(b, "data-price"); },
    "price-desc": function (a, b) { return num(b, "data-price") - num(a, "data-price"); }
  };

  function apply() {
    var brand = brandSel.value;
    var type = typeSel.value;
    var sorter = sorters[sortSel.value] || sorters.discount;

    cards.sort(sorter);

    var visible = 0;
    cards.forEach(function (card) {
      var show = str(card, "data-gender") === gender &&
                 (!brand || str(card, "data-brand") === brand) &&
                 (!type || str(card, "data-type") === type);
      card.hidden = !show;
      if (show) visible++;
      grid.appendChild(card);
    });

    if (countEl) countEl.textContent = visible;
    if (emptyEl) emptyEl.hidden = visible !== 0;
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      gender = tab.getAttribute("data-gender");
      tabs.forEach(function (t) { t.classList.toggle("is-active", t === tab); });
      apply();
    });
  });
  [sortSel, brandSel, typeSel].forEach(function (el) {
    if (el) el.addEventListener("change", apply);
  });
  apply();
})();
