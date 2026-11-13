"""本机专用证书（跨平台，取代 mkcert.sh；不依赖 openssl 命令，也不依赖任何外部服务）。

  根证书 ca.crt：只生成一次，用户在手机上信任一次即可（iOS 描述文件 / 安卓 CA 证书）。
                 已有的（包括 mkcert.sh 生成的）一律沿用，手机不必重新信任。
  服务器证书 server.crt：按当前局域网 IP 签发；IP 变了、快过期了就自动重签，手机无需任何操作。

证书参数按 iOS 13+ 对 TLS 服务器证书的要求：RSA 2048、SAN、EKU serverAuth、有效期 ≤ 825 天。

安全：根证书带「名称限制」，只能给局域网 / 本机地址签证书。即使电脑上的根证书私钥被偷，
也没法拿它冒充任何正常网站。早期版本生成的根证书没有这个限制，会自动换成新的（手机要重新信任一次）。
"""
import datetime
import ipaddress
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

SERVER_DAYS = 825        # iOS 硬性要求：服务器证书「总有效期」（起止之差）不得超过 825 天，否则直接拒绝连接
BACKDATE = datetime.timedelta(hours=1)   # 起始时间略往前，容忍手机时钟稍慢；总长仍是 825 天
RENEW_BEFORE_DAYS = 30
CA_CN = "Open Your Eyes LAN CA"
# 只允许签这些地址：私有网段、运营商级 NAT（含 Tailscale 等）、链路本地、本机
PERMITTED_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8"]


def _ca_constrained(cert):
    try:
        cert.extensions.get_extension_for_class(x509.NameConstraints)
        return True
    except x509.ExtensionNotFound:
        return False


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _write(path, data, private=False):
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    if private:
        os.chmod(path + ".tmp", 0o600)
    os.replace(path + ".tmp", path)


def _new_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())


def _load_key(path):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _load_cert(path):
    with open(path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read())


def _not_after(cert):
    na = getattr(cert, "not_valid_after_utc", None)
    return na if na is not None else cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)


def ensure_ca(cert_dir):
    """返回 (ca_key, ca_cert)；没有就生成一个，有就沿用。"""
    os.makedirs(cert_dir, exist_ok=True)
    try:
        os.chmod(cert_dir, 0o700)
    except OSError:
        pass
    kp, cp = os.path.join(cert_dir, "ca.key"), os.path.join(cert_dir, "ca.crt")
    if os.path.exists(kp) and os.path.exists(cp):
        key, cert = _load_key(kp), _load_cert(cp)
        if _ca_constrained(cert):
            return key, cert
        # 旧根证书没有名称限制：作废（删掉私钥），换新的；服务器证书也随之重签
        for n in ("ca.key", "ca.crt", "server.crt"):
            try:
                os.remove(os.path.join(cert_dir, n))
            except OSError:
                pass
    key = _new_key()
    host = (socket.gethostname() or "computer").split(".")[0][:40]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "%s (%s)" % (CA_CN, host)),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Open Your Eyes (local only)")])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(days=1))
            .not_valid_after(_now() + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .add_extension(x509.NameConstraints(
                permitted_subtrees=[x509.IPAddress(ipaddress.ip_network(n)) for n in PERMITTED_NETS] + [x509.DNSName("localhost")],
                excluded_subtrees=None), critical=True)
            .sign(key, hashes.SHA256()))
    _write(kp, _key_pem(key), private=True)
    _write(cp, cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def ca_needs_upgrade(cert_dir):
    try:
        return not _ca_constrained(_load_cert(os.path.join(cert_dir, "ca.crt")))
    except Exception:
        return False


def ip_allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(a in ipaddress.ip_network(n) for n in PERMITTED_NETS)


def server_cert_status(cert_dir, ips):
    """服务器证书是否可用：存在、覆盖所有 IP、离过期还远。返回 (ok, 原因)。"""
    cp, kp = os.path.join(cert_dir, "server.crt"), os.path.join(cert_dir, "server.key")
    if not (os.path.exists(cp) and os.path.exists(kp)):
        return False, "还没有证书"
    if ca_needs_upgrade(cert_dir):
        return False, "根证书需要升级"
    try:
        cert = _load_cert(cp)
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    except Exception as exc:
        return False, "证书读不出来（%s）" % exc
    missing = [ip for ip in ips if ip not in have]
    if missing:
        return False, "电脑的地址变了（%s）" % "、".join(missing)
    if _not_after(cert) - _now() < datetime.timedelta(days=RENEW_BEFORE_DAYS):
        return False, "证书快过期了"
    nb = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    if _not_after(cert) - nb > datetime.timedelta(days=SERVER_DAYS):
        return False, "证书有效期超过 iPhone 允许的 825 天"
    return True, "正常"


def ensure_server_cert(cert_dir, ips, force=False):
    """按需（重新）签发服务器证书。返回 True 表示这次重签了。"""
    ips = [ip for ip in dict.fromkeys(list(ips) + ["127.0.0.1"]) if ip and ip_allowed(ip)]
    ok, _ = server_cert_status(cert_dir, ips)
    if ok and not force:
        return False
    ca_key, ca_cert = ensure_ca(cert_dir)
    kp = os.path.join(cert_dir, "server.key")
    key = _load_key(kp) if os.path.exists(kp) else _new_key()
    san = [x509.DNSName("localhost")] + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    start = _now() - BACKDATE
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ips[0])]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + datetime.timedelta(days=SERVER_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    if not os.path.exists(kp):
        _write(kp, _key_pem(key), private=True)
    _write(os.path.join(cert_dir, "server.crt"), cert.public_bytes(serialization.Encoding.PEM))
    return True


def cert_info(cert_dir):
    """给设置页「诊断」看的证书概况。"""
    out = {}
    try:
        ca = _load_cert(os.path.join(cert_dir, "ca.crt"))
        out["caName"] = ca.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        out["caSha256"] = ca.fingerprint(hashes.SHA256()).hex()
        out["caFingerprint"] = out["caSha256"][:16]
    except Exception:
        out["caName"] = None
    try:
        sc = _load_cert(os.path.join(cert_dir, "server.crt"))
        out["serverExpires"] = _not_after(sc).strftime("%Y-%m-%d")
        san = sc.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        out["serverIps"] = [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except Exception:
        out["serverExpires"] = None
    return out
