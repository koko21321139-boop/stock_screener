"""moomoo OpenAPI 用の Ed25519 鍵を作る（pip install cryptography が必要）。
公開鍵は moomoo の AppKey 作成画面に、秘密鍵は GitHub の Secrets（MOOMOO_PRIVATE_KEY）に貼る。"""
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization as s
k = ed25519.Ed25519PrivateKey.generate()
print("=== 公開鍵（moomoo に登録）===")
print(k.public_key().public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo).decode())
print("=== 秘密鍵（GitHub Secrets の MOOMOO_PRIVATE_KEY に登録。他人に見せない）===")
print(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()).decode())
