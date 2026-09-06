// Live hot-listings feed. No dependencies, no build step.
//
// The page ships empty and pulls its data from a JSON feed the poller
// force-pushes to its own branch. Nothing is baked in at build time, because
// these listings sell within the hour and anything rendered server-side would
// be stale before you read it.
(function () {
  "use strict";

  var statusEl = document.getElementById("status");
  if (!statusEl) return;

  var FEED_URL = statusEl.getAttribute("data-feed-url");
  var REFRESH_MS = (parseInt(statusEl.getAttribute("data-refresh"), 10) || 60) * 1000;
  // Past this, the poller has almost certainly stopped and we should say so
  // rather than quietly showing hours-old listings as if they were live.
  var STALE_MS = 20 * 60 * 1000;

  var grid = document.getElementById("feed");
  var statusDot = document.getElementById("status-dot");
  var statusText = document.getElementById("status-text");
  var statsEl = document.getElementById("stats");
  var emptyEl = document.getElementById("empty-state");
  var coldEl = document.getElementById("cold-start");
  var countEl = document.getElementById("visible-count");
  var sortSel = document.getElementById("sort");
  var brandSel = document.getElementById("brand");
  var typeSel = document.getElementById("type");
  var hideSold = document.getElementById("hide-sold");
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));

  var feed = null;
  var section = "men";

  // -- formatting ---------------------------------------------------------

  var SYMBOLS = { GBP: "£", EUR: "€", USD: "$" };

  function money(amount, currency) {
    var symbol = SYMBOLS[currency] || "";
    return symbol + (Math.round(amount * 100) / 100).toFixed(2).replace(/\.00$/, "");
  }

  function duration(minutes) {
    if (minutes == null) return "";
    if (minutes < 60) return Math.round(minutes) + "m";
    if (minutes < 1440) return (minutes / 60).toFixed(1) + "h";
    return Math.round(minutes / 1440) + "d";
  }

  function humanize(key) {
    return (key || "").replace(/_/g, " ").replace(/\b\w/g, function (c) {
      return c.toUpperCase();
    });
  }

  // Feed content originates from listings we don't control. Text is always set
  // via textContent, but a URL assigned to href or src is executable if it
  // carries a javascript: scheme, so both are whitelisted to http(s).
  function safeUrl(value) {
    if (!value) return "";
    return /^https?:\/\//i.test(value) ? value : "";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // -- rendering ----------------------------------------------------------

  function card(item) {
    var article = el("article", "card" + (item.sold ? " is-sold" : "") +
                                (item.alerted ? " is-alerted" : ""));

    var link = el("a", "card-link");
    var href = safeUrl(item.url);
    if (href) link.href = href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";

    var imageWrap = el("div", "card-image");
    var imageSrc = safeUrl(item.image_url);
    if (imageSrc) {
      var img = el("img");
      img.src = imageSrc;
      img.alt = (item.brand_title || "") + " " + (item.title || "");
      img.loading = "lazy";
      // Vinted's CDN 404s once a listing is deleted, and a broken <img> renders
      // its alt text sprawling across the card. Swap in the placeholder instead.
      img.addEventListener("error", function () {
        img.remove();
        imageWrap.appendChild(el("div", "no-image", "No image"));
      });
      imageWrap.appendChild(img);
    } else {
      imageWrap.appendChild(el("div", "no-image", "No image"));
    }

    // The headline number: how fast it's being favourited. This is the whole
    // reason the listing is on the page, so it goes on the image, not buried.
    if (item.fav_per_hour != null && item.fav_per_hour > 0) {
      imageWrap.appendChild(
        el("span", "heat-badge", "♥ " + Math.round(item.fav_per_hour) + "/hr")
      );
    }
    if (item.discount_pct) {
      imageWrap.appendChild(
        el("span", "discount-badge", "-" + Math.round(item.discount_pct * 100) + "%")
      );
    }
    if (item.sold) {
      var sold = item.seconds_to_sell != null
        ? "SOLD in " + duration(item.seconds_to_sell / 60)
        : "SOLD";
      imageWrap.appendChild(el("span", "sold-badge", sold));
    } else if (item.alerted) {
      imageWrap.appendChild(el("span", "alert-badge", "ALERTED"));
    }
    link.appendChild(imageWrap);

    // A bar showing where this sits on the heat scale, so the ranking is
    // legible at a glance rather than only implied by position.
    var meter = el("div", "heat-meter");
    var fill = el("span", "heat-fill");
    fill.style.width = Math.max(2, Math.round((item.heat || 0) * 100)) + "%";
    meter.appendChild(fill);
    link.appendChild(meter);

    var body = el("div", "card-body");
    body.appendChild(el("p", "card-brand", item.brand_title || humanize(item.brand)));
    body.appendChild(el("h2", "card-title", item.title));

    var meta = el("p", "card-meta");
    if (item.size) meta.appendChild(el("span", "size", "Size " + item.size));
    if (item.condition) meta.appendChild(el("span", "condition", item.condition));
    meta.appendChild(el("span", "age", duration(item.age_minutes) + " old"));
    body.appendChild(meta);

    var price = el("p", "card-price");
    price.appendChild(el("span", "price", money(item.price, item.currency)));
    if (item.baseline) {
      price.appendChild(
        el("span", "baseline", money(item.baseline, item.currency))
      );
    }
    body.appendChild(price);

    var signals = [];
    if (item.favourites != null) {
      signals.push(item.favourites + (item.favourites === 1 ? " like" : " likes"));
    }
    if (item.views != null) signals.push(item.views + " views");
    if (signals.length) body.appendChild(el("p", "card-signals", signals.join(" · ")));
    if (item.measured === false) {
      body.appendChild(el("p", "card-estimate", "rate estimated from age"));
    }

    link.appendChild(body);
    article.appendChild(link);
    return article;
  }

  var SORTERS = {
    heat: function (a, b) { return (b.heat || 0) - (a.heat || 0); },
    likes: function (a, b) { return (b.fav_per_hour || 0) - (a.fav_per_hour || 0); },
    recent: function (a, b) { return (a.age_minutes || 0) - (b.age_minutes || 0); },
    discount: function (a, b) { return (b.discount_pct || 0) - (a.discount_pct || 0); },
    "price-asc": function (a, b) { return a.price - b.price; },
    "price-desc": function (a, b) { return b.price - a.price; }
  };

  function visibleItems() {
    if (!feed) return [];
    var brand = brandSel.value;
    var type = typeSel.value;
    return feed.items.filter(function (item) {
      if (item.section !== section) return false;
      if (brand && item.brand !== brand) return false;
      if (type && item.type !== type) return false;
      if (hideSold.checked && item.sold) return false;
      return true;
    }).sort(SORTERS[sortSel.value] || SORTERS.heat);
  }

  function render() {
    var items = visibleItems();
    grid.textContent = "";
    items.forEach(function (item) { grid.appendChild(card(item)); });

    countEl.textContent = items.length;
    var haveAny = feed && feed.items.length > 0;
    emptyEl.hidden = items.length > 0 || !haveAny;
    coldEl.hidden = haveAny;

    tabs.forEach(function (tab) {
      var value = tab.getAttribute("data-section");
      var badge = tab.querySelector("[data-count-for]");
      if (!badge || !feed) return;
      badge.textContent = feed.items.filter(function (item) {
        return item.section === value && !(hideSold.checked && item.sold);
      }).length;
    });
  }

  function fillFilters() {
    function options(select, values, label) {
      var current = select.value;
      select.textContent = "";
      select.appendChild(new Option(label, ""));
      values.forEach(function (v) { select.appendChild(new Option(humanize(v), v)); });
      select.value = current;   // keep the user's choice across refreshes
    }
    var brands = {}, types = {};
    feed.items.forEach(function (i) {
      if (i.brand) brands[i.brand] = 1;
      if (i.type) types[i.type] = 1;
    });
    options(brandSel, Object.keys(brands).sort(), "All brands");
    options(typeSel, Object.keys(types).sort(), "All types");
  }

  function renderStats() {
    statsEl.hidden = false;
    document.getElementById("stat-tracked").textContent = feed.counts.tracked;
    document.getElementById("stat-alerted").textContent = feed.counts.alerted;
    document.getElementById("stat-sold").textContent = feed.counts.sold;
    document.getElementById("stat-median").textContent =
      feed.median_seconds_to_sell != null
        ? duration(feed.median_seconds_to_sell / 60)
        : "–";

    var bar = feed.bar || {};
    document.getElementById("stat-bar").textContent =
      (bar.favourites_per_hour != null ? bar.favourites_per_hour : "–") + "/hr";
    document.getElementById("stat-bar-label").textContent =
      "alert bar · " + (bar.adaptive ? "adaptive" : "floor");
  }

  function setStatus(state, text) {
    statusDot.className = "status-dot is-" + state;
    statusText.textContent = text;
  }

  function showFreshness() {
    if (!feed || !feed.generated_at) return;
    var age = Date.now() - Date.parse(feed.generated_at);
    if (isNaN(age)) return;
    if (age > STALE_MS) {
      // Be explicit. Silently showing stale listings as live is the one failure
      // mode that would actually waste the reader's time.
      setStatus("stale", "Feed is " + duration(age / 60000) +
                         " old — the poller may have stopped.");
    } else {
      setStatus("live", "Updated " + duration(Math.max(age, 0) / 60000) + " ago");
    }
  }

  // -- loading ------------------------------------------------------------

  function showFeed(data) {
    feed = data;
    fillFilters();
    renderStats();
    render();
    showFreshness();
  }

  function bootstrap() {
    // Render whatever was inlined at build time, so there's content on first
    // paint rather than a spinner. Live data supersedes it moments later.
    var node = document.getElementById("feed-bootstrap");
    if (!node || !node.textContent.trim()) return;
    try {
      var data = JSON.parse(node.textContent);
      if (data && data.items) {
        showFeed(data);
        if (data.stale_fallback) {
          setStatus("stale", "Showing recent alerts — fetching live data…");
        }
      }
    } catch (e) {
      /* A malformed bootstrap must not stop the live fetch below. */
    }
  }

  function load() {
    if (!FEED_URL) {
      setStatus("error", "No feed configured for this site.");
      return;
    }
    // Cache-bust: the feed is served from a CDN that would otherwise hand back
    // the same copy for minutes at a time.
    fetch(FEED_URL + (FEED_URL.indexOf("?") === -1 ? "?" : "&") + "t=" + Date.now(),
          { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(showFeed)
      .catch(function (err) {
        if (feed) {
          showFreshness();     // keep showing what we have, but flag its age
        } else {
          setStatus("error", "Couldn't load the feed (" + err.message +
                             "). It may not have been published yet.");
        }
      });
  }

  // -- wiring -------------------------------------------------------------

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      tabs.forEach(function (t) { t.classList.remove("is-active"); });
      tab.classList.add("is-active");
      section = tab.getAttribute("data-section");
      render();
    });
  });
  [sortSel, brandSel, typeSel].forEach(function (select) {
    select.addEventListener("change", render);
  });
  hideSold.addEventListener("change", render);

  bootstrap();
  load();
  setInterval(load, REFRESH_MS);
  setInterval(showFreshness, 15000);   // keep "updated Nm ago" honest between fetches
})();
