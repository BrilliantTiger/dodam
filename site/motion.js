/* Dodam introduction page: motion that CSS alone cannot do.
   The page is complete without this file. It only reveals sections as they scroll in,
   counts numbers up, draws the timeline (again whenever the screen changes size, as when a
   phone is folded or unfolded), fills the progress bar where CSS cannot, rests in a hidden tab,
   pauses loops that are off screen, switches the color theme, closes the language
   menu on an outside click and reads the status file again for the section about now. Languages are plain links and are never chosen for the reader. */
(function () {
  "use strict";

  var root = document.documentElement;
  var reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  var canObserve = "IntersectionObserver" in window;

  try {
    var saved = window.localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) {}

  // Sections start hidden only when this script can bring them back.
  if (canObserve) root.classList.add("js");

  function all(selector) {
    return Array.prototype.slice.call(document.querySelectorAll(selector));
  }

  function theme() {
    var button = document.querySelector(".theme");
    if (!button) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      var now = root.getAttribute("data-theme");
      if (!now) now = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
      var next = now === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try {
        window.localStorage.setItem("theme", next);
      } catch (e) {}
    });
  }

  function languages() {
    var menu = document.querySelector(".langs");
    if (!menu) return;
    document.addEventListener("click", function (event) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape" || !menu.open) return;
      menu.open = false;
      menu.querySelector("summary").focus();
    });
  }

  function reveals() {
    var seen = new IntersectionObserver(
      function (entries) {
        var order = 0;
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var delay = order * 80;
          entry.target.style.setProperty("--i", order++);
          entry.target.classList.add("in");
          Array.prototype.forEach.call(entry.target.querySelectorAll("[data-count]"), function (el) {
            countUp(el, delay);
          });
          seen.unobserve(entry.target);
        });
      },
      { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
    );
    all(".reveal, [data-stagger] > *").forEach(function (el) {
      seen.observe(el);
    });
  }

  // The page holds the real figures. A figure drops to zero and counts up only at the moment
  // its block is revealed, so it is never left at zero on screen or on paper.
  function countUp(el, delay) {
    var goal = parseInt(el.getAttribute("data-count"), 10);
    var start = null;
    el.textContent = "0";
    function step(now) {
      if (start === null) start = now + delay;
      var t = Math.min(Math.max(now - start, 0) / 1400, 1);
      el.textContent = String(Math.round(goal * (1 - Math.pow(1 - t, 3))));
      if (t < 1) window.requestAnimationFrame(step);
    }
    window.requestAnimationFrame(step);
  }

  function loops() {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        entry.target.classList.toggle("is-paused", !entry.isIntersecting);
      });
    });
    all("[data-loop]").forEach(function (el) {
      seen.observe(el);
    });
  }

  function scrolling() {
    var line = document.querySelector(".timeline");
    var items = all(".timeline li");
    var cssProgress = window.CSS && CSS.supports && CSS.supports("animation-timeline: scroll()");
    var waiting = false;

    function update() {
      waiting = false;
      if (!cssProgress) {
        var room = root.scrollHeight - window.innerHeight;
        root.style.setProperty("--progress", room > 0 ? Math.min(window.scrollY / room, 1) : 0);
      }
      if (!line) return;
      var mark = window.innerHeight * 0.7;
      var box = line.getBoundingClientRect();
      var p = Math.max(0, Math.min((mark - box.top) / box.height, 1));
      line.style.setProperty("--p", p.toFixed(4));
      items.forEach(function (item) {
        item.classList.toggle("on", item.getBoundingClientRect().top < mark);
      });
    }

    function request() {
      if (waiting) return;
      waiting = true;
      window.requestAnimationFrame(update);
    }

    window.addEventListener("scroll", request, { passive: true });
    window.addEventListener("resize", request, { passive: true });
    window.addEventListener("orientationchange", request, { passive: true });
    // Folding or unfolding a phone changes the size of the page without a reload.
    if ("ResizeObserver" in window) new ResizeObserver(request).observe(root);
    update();
  }

  // When the screen changes width (a phone is folded, unfolded or turned), keep the reader
  // at the same place in the same block instead of at the same number of pixels.
  function anchoring() {
    var blocks = all("main > .hero, section header, .dam, .stats, .grid, .plain, .marquee, .timeline, .rules, .note, footer");
    var width = window.innerWidth;
    var anchor = null;
    var into = 0;

    function remember() {
      anchor = null;
      for (var i = 0; i < blocks.length; i++) {
        var box = blocks[i].getBoundingClientRect();
        if (box.bottom > 1) {
          anchor = blocks[i];
          into = box.top < 0 ? -box.top / box.height : -box.top;
          return;
        }
      }
    }

    function restore() {
      if (window.innerWidth === width) return;
      width = window.innerWidth;
      if (!anchor || window.scrollY < 1) return;
      var box = anchor.getBoundingClientRect();
      var top = into > 0 ? -into * box.height : -into;
      window.scrollBy({ top: box.top - top, behavior: "instant" });
    }

    window.addEventListener("scroll", remember, { passive: true });
    window.addEventListener("resize", restore);
    remember();
  }

  function resting() {
    function set() {
      root.classList.toggle("tab-hidden", document.hidden);
    }
    document.addEventListener("visibilitychange", set);
    set();
  }

  // The section about now. Its values are in the page already; an open page reads the status file
  // from its own address again and sets them as text, never as markup. Nothing is sent anywhere else.
  function live() {
    var box = document.getElementById("live");
    if (!box || !window.fetch || !box.getAttribute("data-status")) return;
    var lang = root.lang || "en";
    var repo = box.getAttribute("data-repo") + "/pull/";

    function read(status, name) {
      return name.split(".").reduce(function (found, part) {
        return found == null ? found : found[part];
      }, status);
    }
    function day(stamp) {
      var date = new Date(String(stamp).slice(0, 10) + "T00:00:00Z");
      if (isNaN(date)) return "";
      try {
        return new Intl.DateTimeFormat(lang, { dateStyle: "long", timeZone: "UTC" }).format(date);
      } catch (e) {
        return String(stamp).slice(0, 10);
      }
    }
    function each(attribute, set) {
      Array.prototype.slice.call(box.querySelectorAll("[" + attribute + "]")).forEach(function (el) {
        set(el, el.getAttribute(attribute));
      });
    }
    function show(status) {
      if (!status || status.version !== 1 || !status.step || !status.charter || !status.tasks) return;
      each("data-live", function (el, name) {
        var found = read(status, name);
        if (found != null) el.textContent = String(found);
      });
      each("data-live-date", function (el, name) {
        var found = read(status, name);
        if (found && day(found)) {
          el.setAttribute("datetime", String(found));
          el.textContent = day(found);
        }
      });
      each("data-live-time", function (el, name) {
        var found = read(status, name);
        if (found && day(found)) {
          el.setAttribute("datetime", String(found));
          el.textContent = day(found) + " " + String(found).slice(11, 16) + " UTC";
        }
      });
      each("data-live-pick", function (el, name) {
        var option = el.getAttribute("data-options").split("|")[read(status, name) - 1];
        if (option) el.textContent = option;
      });
      each("data-live-show", function (el, name) {
        el.hidden = read(status, name) == null;
      });
      each("data-live-list", function (list, name) {
        var rows = read(status, name);
        if (!Array.isArray(rows)) return;
        while (list.firstChild) list.removeChild(list.firstChild);
        rows.slice(0, 5).forEach(function (row) {
          if (!row || typeof row.title !== "string" || !day(row.date)) return;
          var item = document.createElement("li");
          var when = document.createElement("time");
          var link = document.createElement("a");
          when.setAttribute("datetime", String(row.date));
          when.textContent = day(row.date);
          link.className = "link";
          link.lang = "en";
          if (String(row.url).indexOf(repo) === 0) link.href = row.url;
          link.textContent = row.title;
          item.appendChild(when);
          item.appendChild(document.createTextNode(" "));
          item.appendChild(link);
          list.appendChild(item);
        });
      });
      box.hidden = false;
    }
    function load() {
      if (document.hidden) return;
      window
        .fetch(box.getAttribute("data-status"), { cache: "no-store", credentials: "omit" })
        .then(function (response) {
          return response.ok ? response.json() : null;
        })
        .then(show)
        .catch(function () {});
    }
    load();
    window.setInterval(load, 10 * 60 * 1000);
    document.addEventListener("visibilitychange", load);
  }

  function start() {
    try {
      theme();
      languages();
      live();
      if (!canObserve || reduce.matches) {
        root.classList.remove("js");
        return;
      }
      reveals();
      loops();
      resting();
      anchoring();
      scrolling();
    } catch (e) {
      root.classList.remove("js");
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
