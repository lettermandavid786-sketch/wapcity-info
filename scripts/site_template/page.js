// Runs inside each archived page: tells the navigator which page is showing,
// follows its theme, and shows a "back to navigator" strip when opened alone.
(function () {
  var id = document.body.getAttribute("data-id");
  var embedded = window.parent !== window;
  if (!embedded) {
    var bar = document.querySelector(".wc-standalone");
    if (bar) bar.hidden = false;
    return;
  }
  try { window.parent.postMessage({ wc: "nav", id: id }, "*"); } catch (e) {}
  window.addEventListener("message", function (ev) {
    if (ev.data && ev.data.wc === "theme") {
      if (ev.data.theme) document.documentElement.setAttribute("data-theme", ev.data.theme);
      else document.documentElement.removeAttribute("data-theme");
    }
  });
})();
