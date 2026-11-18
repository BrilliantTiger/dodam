/* Dodam introduction page: measurement, only with the visitor's agreement.
   This file is published only when a measurement ID is set. By itself it loads nothing and sets no
   cookie. It shows the question once, in the page's language. Agreeing loads Google's tag for
   Google Analytics, with advertising features off. Declining loads nothing. A browser that sends a
   signal refusing tracking is treated as having declined and is not asked. The choice is kept on
   this device and can be changed from the footer. */
(function () {
  "use strict";

  var tag = document.currentScript;
  var id = tag ? tag.getAttribute("data-id") : "";
  if (!/^G-[A-Z0-9]+$/.test(id || "")) return;

  var KEY = "measure";
  var loaded = false;

  function refused() {
    return navigator.globalPrivacyControl === true || navigator.doNotTrack === "1" || window.doNotTrack === "1";
  }
  function chosen() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }
  function keep(choice) {
    try {
      window.localStorage.setItem(KEY, choice);
    } catch (e) {}
  }

  function measure() {
    // Switched on first, so that agreeing again after declining resumes in the same tab.
    window["ga-disable-" + id] = false;
    if (loaded) return;
    loaded = true;
    window.dataLayer = window.dataLayer || [];
    function gtag() {
      window.dataLayer.push(arguments);
    }
    gtag("consent", "default", {
      ad_storage: "denied",
      ad_user_data: "denied",
      ad_personalization: "denied",
      analytics_storage: "granted"
    });
    gtag("js", new Date());
    gtag("config", id, { allow_google_signals: false, allow_ad_personalization_signals: false });
    var script = document.createElement("script");
    script.async = true;
    script.src = "https://www.googletagmanager.com/gtag/js?id=" + encodeURIComponent(id);
    document.head.appendChild(script);
  }

  // Stop measuring and remove the cookies the measurement set on this site.
  function forget() {
    window["ga-disable-" + id] = true;
    var host = window.location.hostname;
    document.cookie.split(";").forEach(function (pair) {
      var name = pair.split("=")[0].trim();
      if (name.indexOf("_ga") !== 0) return;
      [host, "." + host, "." + host.split(".").slice(-2).join(".")].forEach(function (domain) {
        document.cookie = name + "=; Max-Age=0; path=/; domain=" + domain;
      });
      document.cookie = name + "=; Max-Age=0; path=/";
    });
  }

  function start() {
    var bar = document.getElementById("consent");
    var again = document.querySelector("[data-consent-open]");
    if (!bar || refused()) return;
    // Evaluated a second time on the same page, this script does nothing: the first run has the handlers.
    if (bar.getAttribute("data-measure")) return;
    bar.setAttribute("data-measure", "on");

    Array.prototype.slice.call(bar.querySelectorAll("[data-consent]")).forEach(function (button) {
      button.addEventListener("click", function () {
        var choice = button.getAttribute("data-consent") === "yes" ? "yes" : "no";
        keep(choice);
        bar.hidden = true;
        if (choice === "yes") measure();
        else forget();
      });
    });
    if (again) {
      again.hidden = false;
      again.addEventListener("click", function () {
        bar.hidden = false;
        bar.scrollIntoView();
        var first = bar.querySelector("button");
        if (first) first.focus();
      });
    }

    var choice = chosen();
    if (choice === "yes") measure();
    else if (choice !== "no") bar.hidden = false;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
