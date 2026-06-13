// ノウハウキング 共通ナビ。全ダッシュボードのヘッダーに同じリンク列を注入する。
// 各ページは <script src="/static/nav.js"></script> を読み込むだけ。リンク追加はここ1箇所。
(function () {
  var LINKS = [
    ["/", "ホーム"],
    ["/growth", "成長"],
    ["/daily", "毎日ログ"],
    ["/bucho", "部長別"],
    ["/token-cutter", "コスト"],
    ["/anthropic-cost", "AIコスト"],
    ["/lore", "ロア"],
  ];

  function build() {
    var nav = document.querySelector("nav");
    if (!nav) return;
    var auth = document.getElementById("auth-box");
    // リンクを入れるコンテナ：auth-box の親 → 無ければ nav 内の右側 flex → 最後の手段で nav 直下
    var cont =
      (auth && auth.parentElement) ||
      nav.querySelector(".flex.items-center.gap-2") ||
      nav.querySelector("div > div:last-child") ||
      nav;

    var path = location.pathname.replace(/\/+$/, "") || "/";

    // 既存のページ内リンク（auth-box 内のログアウト等は除く）を消す
    Array.prototype.forEach.call(cont.querySelectorAll("a"), function (a) {
      if (!auth || !auth.contains(a)) a.remove();
    });

    var frag = document.createDocumentFragment();
    LINKS.forEach(function (pair) {
      var href = pair[0],
        label = pair[1];
      var a = document.createElement("a");
      a.href = href;
      a.textContent = label;
      var active = href === "/" ? path === "/" : path === href;
      a.className =
        "text-sm px-3 py-1.5 rounded-lg transition " +
        (active
          ? "bg-indigo-600 text-white"
          : "bg-indigo-50 hover:bg-indigo-100 text-indigo-700");
      frag.appendChild(a);
    });
    cont.insertBefore(frag, auth || null);
  }

  if (document.readyState !== "loading") build();
  else document.addEventListener("DOMContentLoaded", build);
})();
