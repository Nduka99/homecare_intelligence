"""A connection probe run inside a container or pod, shared by stack_check.py and cluster_check.py.

The probe is plain Python passed to `python -c`, so it runs in any image that has Python (the
app's image does). Its arguments are a host and a port number, or a host and "dns" to look the
name up. It prints one line starting with "probe: ". Output without that line means the probe
never ran; the checks count that as a failure, never as a refused connection.
"""

CODE = """
import socket, sys
host, port = sys.argv[1], sys.argv[2]
try:
    if port == "dns":
        socket.getaddrinfo(host, 443)
        print("probe: connected (name resolved)")
    else:
        with socket.create_connection((host, int(port)), timeout=5) as s:
            s.sendall(b"GET /version HTTP/1.0\\r\\nHost: x\\r\\n\\r\\n")
            s.settimeout(5)
            try:
                reply = s.recv(4096)
            except OSError:
                reply = b""
            print("probe: connected engine" if b"ApiVersion" in reply else "probe: connected")
except OSError as error:
    print("probe:", type(error).__name__, error.errno)
"""


def answer(output: str) -> str:
    """The probe's answer from a command's output, or "did not run: ..." if it never ran."""
    lines = output.strip().splitlines()
    for line in lines:
        if line.startswith("probe: "):
            return line.removeprefix("probe: ")
    return "did not run: " + (lines[-1] if lines else "no output")


def refused(text: str) -> bool:
    """The probe ran and its connection or lookup failed."""
    return not text.startswith(("connected", "did not run"))
