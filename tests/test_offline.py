import json
import os
import platform
import socket
import subprocess
import sys
import threading

import pytest

from consumer_agent_testing_tool.offline import kernel_denies_default_egress, policy_for


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS kernel enforcement test")
def test_kernel_allowlist_and_forbidden_destination_canary():
    sockets, ports, hits = [], [], []
    try:
        for index in range(2):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(2)
            sockets.append(listener)
            ports.append(listener.getsockname()[1])

            def accept(sock=listener, number=index):
                try:
                    connection, _ = sock.accept()
                    hits.append(number)
                    connection.close()
                except OSError:
                    pass
            threading.Thread(target=accept, daemon=True).start()
        code = """
import json,socket
results=[]
for port in PORTS:
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.5):
            results.append('connected')
    except OSError:
        results.append('blocked')
print(json.dumps(results))
""".replace("PORTS", repr(ports))
        child = subprocess.run(["sandbox-exec", "-p", policy_for([ports[0]]),
                                sys.executable, "-c", code], capture_output=True, text=True,
                               timeout=5, check=True)
        assert json.loads(child.stdout) == ["connected", "blocked"]
        assert 1 not in hits
    finally:
        for listener in sockets:
            listener.close()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS process-policy introspection")
def test_kernel_policy_for_an_existing_server_process():
    child = subprocess.Popen(["sandbox-exec", "-p", policy_for([]),
        sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(10)"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        assert kernel_denies_default_egress(child.pid)
        assert not kernel_denies_default_egress(os.getpid())
    finally:
        child.terminate()
        child.wait(timeout=5)
