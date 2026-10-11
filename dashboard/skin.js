(function () {
  var KEY = "qb-skin", root = document.documentElement, v = "term";
  try { v = localStorage.getItem(KEY) || "term"; } catch (e) {}
  function apply(x) { if (x === "term") root.setAttribute("data-skin", "term"); else root.removeAttribute("data-skin"); }
  apply(v);
  document.addEventListener("DOMContentLoaded", function () {
    var h = location.hostname;
    if (!(h === "127.0.0.1" || h === "localhost" || h === "[::1]" || h === "::1"))
      document.querySelectorAll("[data-local]").forEach(function (e) { e.remove(); });
    var bar = document.querySelector(".bar"); if (!bar) return;
    var b = document.createElement("button"); b.className = "skinbtn"; b.type = "button";
    function label() { b.textContent = root.getAttribute("data-skin") === "term" ? "科技风" : "经典外观"; }
    b.addEventListener("click", function () {
      v = root.getAttribute("data-skin") === "term" ? "classic" : "term"; apply(v);
      try { localStorage.setItem(KEY, v); } catch (e) {} label();
    });
    label(); bar.appendChild(b);
  });
})();
