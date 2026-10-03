"""仅向域名白名单转发 HTTP/HTTPS 的任务出口代理。"""

from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import os
import select
import socket
import socketserver
import sys
import threading
import time
from typing import cast
from urllib.parse import urlsplit

_FAKE_IP_DNS_RANGE = ipaddress.ip_network("198.18.0.0/15")


class Budget:
    """本代理进程内所有连接共享的累计传输字节预算。"""
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.lock = threading.Lock()

    def charge(self, size: int) -> None:
        # 加锁使不同请求线程不能同时越过同一个总量上限。
        with self.lock:
            self.used += size
            if self.used > self.limit:
                raise ValueError("sandbox network byte limit exceeded")


class ProxyHandler(socketserver.BaseRequestHandler):
    """每条客户端 TCP 连接一个 handler；只支持受控 HTTP 与 HTTPS CONNECT。"""
    @property
    def proxy(self) -> ProxyServer:
        return cast(ProxyServer, self.server)

    def handle(self) -> None:
        """先读取有大小上限的代理请求头，再认证、验证目标并开始转发。"""
        self.request.settimeout(15)
        tunnel_started = False
        upstream: socket.socket | None = None
        header = bytearray()
        while b"\r\n\r\n" not in header:
            # 请求头没有边界或超过 64 KiB 时直接结束连接，避免无界缓存。
            chunk = self.request.recv(4096)
            if not chunk:
                return
            header.extend(chunk)
            if len(header) > 65_536:
                return
        split_at = header.index(b"\r\n\r\n") + 4
        raw_headers = bytes(header[:split_at])
        payload = bytes(header[split_at:])
        stage = "解析请求"
        try:
            lines = raw_headers.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = [line for line in lines[1:] if line]
            stage = "验证代理凭据"
            self._authorize(headers)
            if method.upper() == "CONNECT":
                # HTTPS 隧道先检查 CONNECT 主机，建立连接后还检查 TLS SNI。
                stage = "连接 HTTPS 目标"
                host, port = self._authority(target, 443)
                if port != 443:
                    raise ValueError("HTTPS tunnels are restricted to port 443")
                upstream = self._connect(host, port)
                self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                tunnel_started = True
                stage = "验证 TLS SNI"
                first_tls = self._read_client_hello(host, payload)
                stage = "转发 HTTPS 数据"
                self._relay(upstream, first_tls)
                return
            else:
                # 普通 HTTP 使用绝对 URL；Host 必须与 URL 主机一致。
                stage = "检查 HTTP 目标"
                parsed = urlsplit(target)
                if parsed.scheme != "http" or not parsed.hostname or parsed.fragment:
                    self.request.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    return
                host, port = parsed.hostname, parsed.port or 80
                if port != 80:
                    raise ValueError("plain HTTP proxy requests are restricted to port 80")
                if parsed.username is not None or parsed.password is not None:
                    raise ValueError("URL userinfo is not allowed")
                host_headers = [line.partition(":")[2].strip() for line in headers
                                if line.lower().startswith("host:")]
                if len(host_headers) != 1:
                    raise ValueError("exactly one Host header is required")
                host_header, host_header_port = self._authority(host_headers[0], 80)
                if (host_header.rstrip(".").casefold() != host.rstrip(".").casefold()
                        or host_header_port != port):
                    raise ValueError("Host header does not match the requested destination")
                stage = "连接 HTTP 目标"
                upstream = self._connect(host, port)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                clean_headers = [line for line in headers
                                 if not line.lower().startswith(("proxy-authorization:",
                                                                 "proxy-connection:"))
                                 and not line.lower().startswith("connection:")]
                clean_headers.append("Connection: close")
                # 代理认证头只供本代理使用，不转发给目标网站。
                outbound = f"{method} {path} {version}\r\n" + "\r\n".join(clean_headers)
                outbound += "\r\n\r\n"
                request_bytes = outbound.encode("iso-8859-1") + payload
                self.proxy.budget.charge(len(request_bytes))
                upstream.sendall(request_bytes)
            stage = "转发 HTTP 数据"
            self._relay(upstream)
        except (OSError, ValueError, UnicodeError, IndexError, binascii.Error) as exc:
            # 只记录处理阶段和异常类别/简短原因，不记录 URL、请求头或代理令牌。
            # HTTP 客户端原本只看到统一 403，Docker 日志可帮助区分路由、
            # DNS、白名单与认证失败；日志长度受限，防止异常文本刷满输出。
            print(f"[沙箱代理] {stage}: {type(exc).__name__}: {str(exc)[:160]}",
                  file=sys.stderr, flush=True)
            if upstream is not None:
                upstream.close()
            if not tunnel_started:
                try:
                    self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                except OSError:
                    pass

    def _authorize(self, headers: list[str]) -> None:
        """核对本任务随机代理令牌；每个请求都必须携带唯一认证头。"""
        authorization = [line.partition(":")[2].strip() for line in headers
                         if line.lower().startswith("proxy-authorization:")]
        if not authorization:
            raise ValueError("proxy authorization header is missing")
        if len(authorization) != 1:
            raise ValueError("duplicate proxy authorization headers")
        scheme, separator, encoded = authorization[0].partition(" ")
        if scheme.casefold() != "basic" or not separator:
            raise ValueError("proxy authorization scheme is not Basic")
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, has_password, _password = decoded.partition(":")
        if not has_password or username != self.proxy.token:
            raise ValueError("proxy token does not match")

    @staticmethod
    def _authority(value: str, default_port: int) -> tuple[str, int]:
        parsed = urlsplit("//" + value)
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("invalid proxy authority")
        return parsed.hostname, parsed.port or default_port

    def _connect(self, host: str, port: int) -> socket.socket:
        """确认域名白名单与端口，再解析并只连接公网 IP，返回上游 socket。

        DNS 解析后的地址本身也要检查，防止白名单域名解析到本机/内网。
        connect 使用检查过的地址，避免检查后再次解析成另一目的地。
        """
        hostname = host.rstrip(".").casefold()
        if port not in {80, 443} or not self.proxy.allowed(hostname):
            raise ValueError("destination is not allowed")
        try:
            addresses = [ipaddress.ip_address(hostname)]
        except ValueError:
            addresses = [ipaddress.ip_address(item[4][0])
                         for item in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]
        # 某些宿主透明代理为公网域名返回 198.18/15 虚拟 IPv4。只有宿主策略
        # 明确启用时才信任该保留段；回环、RFC1918、链路本地等始终拒绝。
        if not addresses or any(
            not address.is_global
            and not (self.proxy.trust_fake_ip_dns and address in _FAKE_IP_DNS_RANGE)
            for address in addresses
        ):
            raise ValueError("destination resolved to a non-public address")
        last_error: OSError | None = None
        for address in addresses:
            try:
                family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
                upstream = socket.socket(family, socket.SOCK_STREAM)
                upstream.settimeout(15)
                sockaddr = ((str(address), port, 0, 0) if address.version == 6
                            else (str(address), port))
                upstream.connect(sockaddr)
                return upstream
            except OSError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise ValueError("destination could not be resolved")

    def _read_client_hello(self, host: str, initial: bytes) -> bytes:
        """读取并解析 TLS ClientHello，要求 SNI 与 CONNECT 主机一致。

        返回已读到的原始字节供后续 relay 转发；代理只看握手中的主机名，
        不解密 TLS 应用数据。缺少 SNI、握手过大或主机不符则拒绝隧道。
        """
        wire = bytearray(initial)
        cursor = 0
        handshake = bytearray()

        def read_wire(required: int) -> None:
            while len(wire) - cursor < required:
                chunk = self.request.recv(4096)
                if not chunk:
                    raise ValueError("TLS ClientHello ended early")
                wire.extend(chunk)

        while len(handshake) < 4 or len(handshake) < 4 + int.from_bytes(handshake[1:4], "big"):
            # TLS 握手可能跨多个 record；按 record 长度拼够一份 ClientHello。
            read_wire(5)
            content_type = wire[cursor]
            record_size = int.from_bytes(wire[cursor + 3:cursor + 5], "big")
            if content_type != 22 or record_size > 18_432:
                raise ValueError("HTTPS tunnel must start with a TLS handshake")
            read_wire(5 + record_size)
            handshake.extend(wire[cursor + 5:cursor + 5 + record_size])
            cursor += 5 + record_size
            if len(handshake) > 65_536:
                raise ValueError("TLS ClientHello exceeds the size limit")
        if handshake[0] != 1:
            raise ValueError("TLS tunnel did not start with ClientHello")
        body_size = int.from_bytes(handshake[1:4], "big")
        body = memoryview(handshake)[4:4 + body_size]
        index = 2 + 32
        session_size = body[index]
        index += 1 + session_size
        cipher_size = int.from_bytes(body[index:index + 2], "big")
        index += 2 + cipher_size
        compression_size = body[index]
        index += 1 + compression_size
        extensions_size = int.from_bytes(body[index:index + 2], "big")
        index += 2
        extension_end = index + extensions_size
        sni: str | None = None
        while index + 4 <= extension_end:
            # extension type 0 是 server_name；只接受其中的 host_name 项。
            extension_type = int.from_bytes(body[index:index + 2], "big")
            extension_size = int.from_bytes(body[index + 2:index + 4], "big")
            index += 4
            extension = body[index:index + extension_size]
            index += extension_size
            if extension_type == 0:
                names_size = int.from_bytes(extension[:2], "big")
                name_index = 2
                names_end = min(len(extension), 2 + names_size)
                while name_index + 3 <= names_end:
                    name_type = extension[name_index]
                    name_size = int.from_bytes(extension[name_index + 1:name_index + 3], "big")
                    name_index += 3
                    name = bytes(extension[name_index:name_index + name_size])
                    name_index += name_size
                    if name_type == 0:
                        sni = name.decode("ascii").rstrip(".").casefold()
                        break
        if not sni or sni != host.rstrip(".").casefold():
            raise ValueError("TLS SNI does not match the allowlisted CONNECT host")
        return bytes(wire)

    def _relay(self, upstream: socket.socket, initial: bytes = b"") -> None:
        """双向转发连接；受共享字节预算、空闲时间与总持续时间约束。"""
        upstream.settimeout(self.proxy.idle_timeout)
        self.request.settimeout(self.proxy.idle_timeout)
        peers = (self.request, upstream)
        readable_peers = set(peers)
        deadline = time.monotonic() + self.proxy.max_duration
        try:
            if initial:
                self.proxy.budget.charge(len(initial))
                upstream.sendall(initial)
            while readable_peers and time.monotonic() < deadline:
                readable, _, _ = select.select(tuple(readable_peers), [], [], 1)
                if not readable:
                    continue
                for source in readable:
                    chunk = source.recv(65_536)
                    if not chunk:
                        readable_peers.remove(source)
                        destination = upstream if source is self.request else self.request
                        try:
                            destination.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    self.proxy.budget.charge(len(chunk))
                    destination = upstream if source is self.request else self.request
                    destination.sendall(chunk)
        finally:
            upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    """代理独立容器中的监听器；任务容器只能经内部网络访问它。"""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, allowlist: tuple[str, ...], token: str, max_bytes: int,
                 idle_timeout: int, max_duration: int,
                 trust_fake_ip_dns: bool = False) -> None:
        self.allowlist = allowlist
        self.token = token
        self.budget = Budget(max_bytes)
        self.idle_timeout = idle_timeout
        self.max_duration = max_duration
        self.trust_fake_ip_dns = trust_fake_ip_dns
        super().__init__(("0.0.0.0", 3128), ProxyHandler)

    def allowed(self, hostname: str) -> bool:
        """精确域名或 *.后缀匹配；通配符不包含后缀域名本身。"""
        if not hostname or hostname.startswith("["):
            return False
        for entry in self.allowlist:
            if entry.startswith("*."):
                domain = entry[2:]
                if hostname.endswith("." + domain):
                    return True
            elif hostname == entry:
                return True
        return False


def main() -> int:
    """读取宿主创建代理容器时注入的白名单、预算和随机令牌。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowlist", required=True)
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--idle-timeout", type=int, default=30)
    parser.add_argument("--max-duration", type=int, default=1800)
    parser.add_argument("--trust-fake-ip-dns", action="store_true")
    args = parser.parse_args()
    try:
        allowlist = tuple(json.loads(args.allowlist))
        if not allowlist or any(not isinstance(item, str) for item in allowlist):
            return 2
        token = os.environ["STUDY_CODER_PROXY_TOKEN"]
        if len(token) < 32:
            return 2
        server = ProxyServer(allowlist, token, args.max_bytes,
                             args.idle_timeout, args.max_duration,
                             args.trust_fake_ip_dns)
    except (KeyError, ValueError, OSError):
        return 2
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
