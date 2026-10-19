"""HTTP fixture servers that do not reverse-resolve their bound address."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class FixtureHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer calls getfqdn here. A local fixture needs no hostname lookup,
        # which can block startup for tens of seconds on an offline or CI host.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
