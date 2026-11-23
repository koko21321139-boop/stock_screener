#!/usr/bin/env python3
"""スクリーニング通過銘柄を moomoo のウォッチリスト（自分で作ったグループ）に同期する。

moomoo OpenAPI（HTTP 版、API キー方式）を使う。売買の権限は使わない。
  - 当日のトレンドテンプレート通過銘柄をグループに追加
  - 通過しなくなった日本株はグループから外す（グループ内の米国株などには触れない）

環境変数（GitHub の Secrets / Variables）
  MOOMOO_APP_KEY      AppKey ID（Secrets）
  MOOMOO_PRIVATE_KEY  AppKey 作成時に公開鍵と対にした秘密鍵（PEM。Secrets）
  MOOMOO_GROUP        同期先のグループ名（Variables。既定「スクリーナー」）。moomoo アプリで先に作っておく
  MOOMOO_MAX          入れる銘柄数の上限（RS の高い順。既定 100）
  MOOMOO_MIN_TT       何条件以上で入れるか（既定 8 ＝ 全条件通過）
キーが無ければ何もせず終了する（スクリーニング本体には影響しない）。
"""
import base64, hashlib, json, os, secrets, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get("MOOMOO_BASE") or "https://webapi.moomoo.com"
APP_KEY = os.environ.get("MOOMOO_APP_KEY", "").strip()
PRIV = os.environ.get("MOOMOO_PRIVATE_KEY", "").strip()
GROUP = os.environ.get("MOOMOO_GROUP") or "スクリーナー"
MAX = int(os.environ.get("MOOMOO_MAX") or 100)
MIN_TT = int(os.environ.get("MOOMOO_MIN_TT") or 8)
PREFIX = os.environ.get("MOOMOO_JP_PREFIX") or "JP."

ERRORS = {-3: "パラメータ不正（グループ名が無い・間違っている可能性）", -7: "銘柄コードが認識されない", -8: "このグループは変更できない（システムのグループ）",
          -9: "権限がない（AppKey の権限・ユーザー認証を確認）", -12006: "時刻のずれが大きい"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Moomoo:
    def __init__(self, app_key, pem):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
        key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
        if isinstance(key, ed25519.Ed25519PrivateKey):
            self._sign = key.sign
        elif isinstance(key, rsa.RSAPrivateKey):
            self._sign = lambda m: key.sign(m, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise SystemExit("秘密鍵は Ed25519 か RSA にしてください")
        self.app_key = app_key
        self.offset = 0
        try:   # サーバー時刻との差（5 秒以上ずれると拒否される）
            raw = urllib.request.urlopen(f"{BASE}/api/v1.0/server-time", timeout=20).read()
            self.offset = int(json.loads(raw)["server_time_ms"]) - int(time.time() * 1000)
        except Exception as e:
            log("  サーバー時刻を取得できませんでした（端末の時刻を使います）", e)

    def call(self, method, path, query=None, body=None):
        qs = urllib.parse.urlencode(query or {}, quote_via=urllib.parse.quote)
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode() if body is not None else b""
        ts = str(int(time.time() * 1000) + self.offset)
        digest = hashlib.sha256(raw).hexdigest() if raw else ""
        sig = base64.b64encode(self._sign("\n".join([ts, method, path, qs, digest]).encode())).decode()
        req = urllib.request.Request(f"{BASE}{path}" + (f"?{qs}" if qs else ""), data=raw or None, method=method, headers={
            "X-Api-Key": self.app_key, "X-Timestamp": ts, "X-Nonce": secrets.token_hex(16), "Authorization": sig,
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                js = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                js = json.loads(e.read())
            except Exception:
                raise RuntimeError(f"HTTP {e.code} {path}") from None
        code = js.get("ret_code")
        if code != 0:
            raise RuntimeError(f"{path}: {code} {ERRORS.get(code, '')} {js.get('ret_msg', '')}".strip())
        data = js.get("data") or {}
        if isinstance(data, dict) and data.get("result_code") not in (None, 0):
            raise RuntimeError(f"{path}: result_code {data.get('result_code')} {js.get('ret_msg', '')}")
        return data


def targets():
    d = json.loads((ROOT / "data" / "screen.json").read_text("utf-8"))
    items = [i for i in d["items"] if i["ttPass"] >= MIN_TT]
    items.sort(key=lambda i: -(i.get("rs") or 0))
    return d["asof"], [PREFIX + i["code"] for i in items[:MAX]]


def main():
    if not APP_KEY or not PRIV:
        log("moomoo のキーが未設定なので同期は省略")
        return
    asof, want = targets()
    m = Moomoo(APP_KEY, PRIV)
    have = [s["code"] for s in m.call("GET", "/api/v1.0/quote/user-security", {"group_name": GROUP}).get("security_list", [])]
    mine = {c for c in have if c.startswith(PREFIX)}      # 日本株だけを管理対象にする
    add = [c for c in want if c not in mine]
    drop = sorted(mine - set(want))
    log(f"{asof}：グループ「{GROUP}」 現在 {len(mine)} 銘柄 → 追加 {len(add)} / 外す {len(drop)}")
    failed = []
    for i in range(0, len(add), 200):
        chunk = add[i:i + 200]
        try:
            m.call("POST", "/api/v1.0/quote/modify-user-security", body={"group_name": GROUP, "op": "ADD", "code_list": chunk})
        except RuntimeError as e:
            if "-7" not in str(e):
                raise
            # どれかのコードが認識されない → 1 銘柄ずつ入れ直して、だめなものだけ飛ばす
            for c in chunk:
                try:
                    m.call("POST", "/api/v1.0/quote/modify-user-security", body={"group_name": GROUP, "op": "ADD", "code_list": [c]})
                except RuntimeError:
                    failed.append(c)
    if drop:
        for i in range(0, len(drop), 200):
            m.call("POST", "/api/v1.0/quote/modify-user-security",
                   body={"group_name": GROUP, "op": "MOVE_OUT", "code_list": drop[i:i + 200]})
    if failed:
        log(f"  moomoo で認識されなかった銘柄 {len(failed)}：{' '.join(failed[:20])}")
        if len(failed) == len(add) and add:
            raise SystemExit(f"どの銘柄も追加できませんでした。日本株のコード形式（{PREFIX}7203 など）が違う可能性があります")
    log("同期しました")


if __name__ == "__main__":
    main()
