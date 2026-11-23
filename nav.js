// ロング⇔ショート切り替えボタン（画面右下に固定表示）
(function () {
  var isShort = /short\.html$/.test(location.pathname);
  var a = document.createElement("a");
  a.href = isShort ? "./" : "short.html";
  a.textContent = isShort ? "📈 ロング" : "📉 ショート";
  a.setAttribute("style", [
    "position:fixed",
    "right:14px",
    "bottom:calc(16px + env(safe-area-inset-bottom))",
    "z-index:9999",
    "padding:10px 16px",
    "border-radius:999px",
    "font:600 15px -apple-system,BlinkMacSystemFont,'Hiragino Sans',sans-serif",
    "text-decoration:none",
    "color:#fff",
    "background:" + (isShort ? "#1a7f37" : "#b42318"),
    "box-shadow:0 4px 14px rgba(0,0,0,.25)"
  ].join(";"));
  document.body.appendChild(a);
})();
