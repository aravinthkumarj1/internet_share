"""
Lightweight SOCKS5/TCP proxy for NAT-less internet sharing.
Used as a fallback when all kernel-level NAT methods fail.

Runs on the gateway adapter (192.168.137.1) and proxies all TCP traffic
from connected clients through the internet-connected adapter.
Also provides DNS forwarding (UDP port 53).
"""
import asyncio
import socket
import struct
import threading
import logging

logger = logging.getLogger("proxy")

# DNS forwarding server
DNS_SERVERS = ["8.8.8.8", "8.8.4.4"]


class NATProxy:
    """
    Lightweight TCP + DNS forwarder.
    - Listens on all TCP ports via a transparent proxy approach
    - Forwards DNS (UDP 53) queries
    """

    def __init__(self, listen_ip="192.168.137.1", log_callback=None):
        self.listen_ip = listen_ip
        self.log_callback = log_callback
        self._running = False
        self._loop = None
        self._thread = None
        self._servers = []
        self._dns_transport = None

    def _log(self, msg):
        if self.log_callback:
            self.log_callback(f"  [Proxy] {msg}")
        logger.info(msg)

    def start(self):
        """Start the proxy in a background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        """Event loop for the proxy."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._start_servers())
            self._log(f"Proxy running on {self.listen_ip}")
            self._loop.run_forever()
        except Exception as e:
            self._log(f"Proxy error: {e}")
        finally:
            self._loop.close()

    async def _start_servers(self):
        """Start DNS forwarder and SOCKS-like proxy."""
        # DNS forwarder on port 53
        try:
            transport, protocol = await self._loop.create_datagram_endpoint(
                lambda: DNSForwarder(self._log),
                local_addr=(self.listen_ip, 53)
            )
            self._dns_transport = transport
            self._log("DNS forwarder started on :53")
        except OSError as e:
            self._log(f"DNS forwarder failed (port 53 in use?): {e}")

        # SOCKS5 proxy on port 1080
        try:
            server = await asyncio.start_server(
                self._handle_socks5,
                self.listen_ip, 1080
            )
            self._servers.append(server)
            self._log("SOCKS5 proxy started on :1080")
        except OSError as e:
            self._log(f"SOCKS5 proxy failed: {e}")

        # HTTP proxy on port 8080
        try:
            server = await asyncio.start_server(
                self._handle_http_proxy,
                self.listen_ip, 8080
            )
            self._servers.append(server)
            self._log("HTTP proxy started on :8080")
        except OSError as e:
            self._log(f"HTTP proxy failed: {e}")

    async def _handle_socks5(self, reader, writer):
        """Handle SOCKS5 proxy connection."""
        try:
            # SOCKS5 greeting
            data = await asyncio.wait_for(reader.read(256), timeout=10)
            if not data or data[0] != 0x05:
                writer.close()
                return

            # No auth required
            writer.write(b'\x05\x00')
            await writer.drain()

            # Connection request
            data = await asyncio.wait_for(reader.read(256), timeout=10)
            if not data or len(data) < 7:
                writer.close()
                return

            cmd = data[1]
            if cmd != 0x01:  # Only CONNECT
                writer.write(b'\x05\x07\x00\x01' + b'\x00' * 6)
                await writer.drain()
                writer.close()
                return

            atyp = data[3]
            if atyp == 0x01:  # IPv4
                dst_addr = socket.inet_ntoa(data[4:8])
                dst_port = struct.unpack('!H', data[8:10])[0]
            elif atyp == 0x03:  # Domain
                domain_len = data[4]
                domain = data[5:5 + domain_len].decode()
                dst_port = struct.unpack('!H', data[5 + domain_len:7 + domain_len])[0]
                dst_addr = domain
            elif atyp == 0x04:  # IPv6
                writer.write(b'\x05\x08\x00\x01' + b'\x00' * 6)
                await writer.drain()
                writer.close()
                return
            else:
                writer.close()
                return

            # Connect to destination
            try:
                remote_reader, remote_writer = await asyncio.wait_for(
                    asyncio.open_connection(dst_addr, dst_port),
                    timeout=15
                )
            except Exception:
                writer.write(b'\x05\x05\x00\x01' + b'\x00' * 6)
                await writer.drain()
                writer.close()
                return

            # Success response
            writer.write(b'\x05\x00\x00\x01' + b'\x00' * 4 + b'\x00\x00')
            await writer.drain()

            # Relay data bidirectionally
            await self._relay(reader, writer, remote_reader, remote_writer)

        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_http_proxy(self, reader, writer):
        """Handle HTTP CONNECT proxy."""
        try:
            # Read the request line
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                writer.close()
                return

            request = line.decode('utf-8', errors='ignore').strip()
            parts = request.split()

            if len(parts) < 3:
                writer.close()
                return

            method = parts[0].upper()

            # Read remaining headers
            while True:
                hdr = await asyncio.wait_for(reader.readline(), timeout=5)
                if hdr == b'\r\n' or hdr == b'\n' or not hdr:
                    break

            if method == 'CONNECT':
                # HTTPS tunnel
                host_port = parts[1]
                if ':' in host_port:
                    host, port = host_port.rsplit(':', 1)
                    port = int(port)
                else:
                    host = host_port
                    port = 443

                try:
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(host, port),
                        timeout=15
                    )
                except Exception:
                    writer.write(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                    await writer.drain()
                    writer.close()
                    return

                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                await self._relay(reader, writer, remote_reader, remote_writer)
            else:
                # Regular HTTP — forward the request
                # Parse URL
                url = parts[1]
                if url.startswith('http://'):
                    url = url[7:]
                    slash = url.find('/')
                    if slash == -1:
                        host_port = url
                        path = '/'
                    else:
                        host_port = url[:slash]
                        path = url[slash:]

                    if ':' in host_port:
                        host, port = host_port.rsplit(':', 1)
                        port = int(port)
                    else:
                        host = host_port
                        port = 80

                    try:
                        remote_reader, remote_writer = await asyncio.wait_for(
                            asyncio.open_connection(host, port),
                            timeout=15
                        )
                    except Exception:
                        writer.write(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                        await writer.drain()
                        writer.close()
                        return

                    # Forward the request
                    new_request = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                    remote_writer.write(new_request.encode())
                    await remote_writer.drain()

                    # Relay response
                    await self._relay(reader, writer, remote_reader, remote_writer)
                else:
                    writer.write(b'HTTP/1.1 400 Bad Request\r\n\r\n')
                    await writer.drain()

        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _relay(self, reader1, writer1, reader2, writer2):
        """Relay data between two connections."""
        async def forward(src, dst):
            try:
                while True:
                    data = await asyncio.wait_for(src.read(8192), timeout=300)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            forward(reader1, writer2),
            forward(reader2, writer1),
            return_exceptions=True
        )

    def stop(self):
        """Stop the proxy."""
        self._running = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._dns_transport:
            try:
                self._dns_transport.close()
            except Exception:
                pass
        for srv in self._servers:
            srv.close()


class DNSForwarder(asyncio.DatagramProtocol):
    """Forwards DNS queries to upstream DNS servers."""

    def __init__(self, log_func=None):
        self._log = log_func or (lambda m: None)
        self._transport = None
        self._pending = {}  # txn_id -> (client_addr, transport)

    def connection_made(self, transport):
        self._transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        txn_id = data[:2]

        # Forward to upstream DNS
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(5)
            sock.sendto(data, (DNS_SERVERS[0], 53))
            response, _ = sock.recvfrom(4096)
            sock.close()
            # Send response back to client
            self._transport.sendto(response, addr)
        except Exception:
            # Try secondary DNS
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.settimeout(5)
                sock.sendto(data, (DNS_SERVERS[1], 53))
                response, _ = sock.recvfrom(4096)
                sock.close()
                self._transport.sendto(response, addr)
            except Exception:
                pass
