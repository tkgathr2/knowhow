// ノウハウキング 共通ナビ。全ダッシュボードのヘッダーに同じリンク列を注入する。
// 各ページは <script src="/static/nav.js"></script> を読み込むだけ。リンク追加はここ1箇所。
(function () {
  // 日報・日報一覧・毎日ログは「日報」ハブに集約（ハブ内のサブタブで切替＝nippou-tabs.js）。
  var LINKS = [
    ["/", "ホーム"],
    ["/nippou/index", "日報"],
    ["/growth", "成長"],
    ["/bucho", "部長別"],
    ["/token-cutter", "コスト"],
    ["/anthropic-cost", "AIコスト"],
    ["/cost-cutter", "削減率"],
    ["/lore", "ロア"],
  ];

  // 「日報」ハブ配下（/nippou・/nippou/index・/daily）はどれを開いていても「日報」を点灯。
  function isActive(href, path) {
    if (href === "/") return path === "/";
    if (href === "/nippou/index") return path.indexOf("/nippou") === 0 || path === "/daily";
    return path === href;
  }

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
      var active = isActive(href, path);
      a.className =
        "text-sm px-3 py-1.5 rounded-lg transition whitespace-nowrap shrink-0 " +
        (active
          ? "bg-indigo-600 text-white"
          : "bg-indigo-50 hover:bg-indigo-100 text-indigo-700");
      frag.appendChild(a);
    });
    cont.insertBefore(frag, auth || null);

    // 横一列で気持ちよく並べる：コンテナは折り返し可（はみ出たら丸ごと次行へ）、
    // 各ボタンは縮めず・文字を途中で折り返さない（"立て"に潰れるのを防ぐ）。
    cont.classList.add("flex", "flex-wrap", "items-center", "justify-end", "gap-2", "gap-y-2");
    Array.prototype.forEach.call(cont.children, function (el) {
      el.classList.add("shrink-0", "whitespace-nowrap");
    });
  }


  // カイゼンくんウィジェット：全ページに表示（knowhow は認証後のみアクセス可）
  (function() {
    if (document.querySelector('script[data-kaizen-knowhow]')) return;
    var s = document.createElement('script');
    s.src = 'https://kaizen.takagi.bz/widget.js';
    s.setAttribute('data-sys', 'knowhow');
    s.setAttribute('data-kaizen-knowhow', '1');
    s.defer = true;
    document.head.appendChild(s);
  })();

  if (document.readyState !== "loading") build();
  else document.addEventListener("DOMContentLoaded", build);
})();