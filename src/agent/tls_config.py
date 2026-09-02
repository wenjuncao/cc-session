# -*- coding: utf-8 -*-
"""
tls_config.py —— B级传输安全：自签证书 + 指纹固定(TOFU)。

职责（CONTRACT §6 T4）：
  - ensure_cert() -> (cert_file, key_file)：首启用 cryptography 生成自签证书（SAN 含本机
    IP/主机名/localhost）到 paths.CERTS_DIR，路径记入配置；已存在则复用。
  - fingerprint() -> str：本机证书 sha256 指纹（小写十六进制）。
  - ssl_context() -> ssl.SSLContext：供 waitress 起 wss / 客户端连使用。
  - pin_check(remote_fp, peer) -> bool：TOFU，首见记录、再见比对、变更返回 False。
    指纹库存 config.ini/DB 的 pinned_fps（json）。
"""
import datetime
import json
import os
import socket
import ssl

from . import paths
from . import config_loader
from . import auth  # 复用 cfg_get/cfg_set（auth 不反向依赖本模块，无循环）

_CERT = os.path.join(paths.CERTS_DIR, "node.crt")
_KEY = os.path.join(paths.CERTS_DIR, "node.key")


def _local_ips():
    ips = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            addr = info[4][0]
            if ":" not in addr:  # 只取 IPv4，简化
                ips.add(addr)
    except Exception:
        pass
    return ips


def _generate(cert_path, key_path):
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname() or "remote-disp"

    sans = [x509.DNSName(hostname), x509.DNSName("localhost")]
    for ip in _local_ips():
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:
            pass

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "remote-disp"),
    ])
    now = datetime.datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _save_paths(cert_path, key_path):
    auth.cfg_set("cert_file", cert_path)
    auth.cfg_set("key_file", key_path)


def ensure_cert():
    # 配置里显式指定的优先
    cert = config_loader.get("security", "cert_file", "") or auth.cfg_get("cert_file")
    key = config_loader.get("security", "key_file", "") or auth.cfg_get("key_file")
    if cert and key and os.path.exists(cert) and os.path.exists(key):
        return cert, key
    if os.path.exists(_CERT) and os.path.exists(_KEY):
        _save_paths(_CERT, _KEY)
        return _CERT, _KEY
    _generate(_CERT, _KEY)
    _save_paths(_CERT, _KEY)
    return _CERT, _KEY


def fingerprint(cert_file=None):
    """返回证书 sha256 指纹（小写十六进制）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    cert_file = cert_file or ensure_cert()[0]
    with open(cert_file, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    return cert.fingerprint(hashes.SHA256).hex()


def ssl_context():
    """服务端 SSLContext（waitress wss）。"""
    cert, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    return ctx


def pin_check(remote_fp, peer="default"):
    """TOFU 指纹固定：首见记录返回 True；再见相同返回 True；变更返回 False。"""
    if not remote_fp:
        return False
    raw = auth.cfg_get("pinned_fps") or "{}"
    try:
        store = json.loads(raw)
    except Exception:
        store = {}
    cur = store.get(peer)
    if cur is None:
        store[peer] = remote_fp
        auth.cfg_set("pinned_fps", json.dumps(store, ensure_ascii=False))
        return True
    return cur == remote_fp
