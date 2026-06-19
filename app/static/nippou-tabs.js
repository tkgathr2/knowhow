// 日報ハブの共通サブタブ（一覧 / 日報 / 毎日ログ）。各ページは
// <script src="/static/nippou-tabs.js"></script> を読み込むだけで、<nav> の直下に
// 同じ切替バーが入る。リンク追加・並び替えはここ1箇所。
(function () {
  var TABS = [
    ["/nippou/index", "📋 一覧"],
    ["/nippou", "📄 日報"],
    ["/daily", "📈 毎日ログ"],
  ];

  function build() {
    var nav = document.querySelector("nav");
    if (!nav || document.getElementById("nippou-subtabs")) return;

    var path = location.pathname.replace(/\/+$/, "") || "/";
    var wrap = document.createElement("div");
    wrap.id = "nippou-subtabs";
    wrap.className = "bg-white border-b border-gray-100 sticky top-0 z-40";

    var inner = document.createElement("div");
    inner.className =
      "max-w-6xl mx-auto px-4 py-2 flex items-center gap-1.5 overflow-x-auto";

    var lead = document.createElement("span");
    lead.className = "text-xs text-gray-400 mr-1 shrink-0";
    lead.textContent = "日報";
    inner.appendChild(lead);

    TABS.forEach(function (t) {
      var href = t[0],
        label = t[1];
      var a = document.createElement("a");
      a.href = href;
      a.textContent = label;
      var active = path === href;
      a.className =
        "text-sm px-4 py-1.5 rounded-lg whitespace-nowrap shrink-0 transition font-semibold " +
        (active
          ? "bg-indigo-600 text-white"
          : "bg-gray-100 hover:bg-gray-200 text-gray-600");
      inner.appendChild(a);
    });

    wrap.appendChild(inner);
    nav.parentNode.insertBefore(wrap, nav.nextSibling);
  }

  if (document.readyState !== "loading") build();
  else document.addEventListener("DOMContentLoaded", build);
})();
