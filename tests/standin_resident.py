"""A stand-in resident for test_app.py's ack tests, run as its own process
so the launcher under test can block waiting for it.

    python standin_resident.py NAME old     # reads, then hangs up (pre-editors)
    python standin_resident.py NAME slow    # reads, answers after 1.5s

Prints "ready" once listening, then one "payload <bytes>" line per request.
A probe -- a connection that sends nothing -- is hung up on and not printed.
"""

import sys
import time

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalServer

ACK = b"K"
SLOW_ACK_S = 1.5


def main() -> int:
    name, mode = sys.argv[1], sys.argv[2]
    qt = QCoreApplication([])
    server = QLocalServer()
    QLocalServer.removeServer(name)
    if not server.listen(name):
        print("could not listen", flush=True)
        return 1

    def accept() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return
        received = connection.readAll().data() if connection.waitForReadyRead(200) else b""
        if received:
            print(f"payload {received!r}", flush=True)
            if mode == "slow":
                time.sleep(SLOW_ACK_S)
                connection.write(ACK)
                connection.waitForBytesWritten(1000)
        connection.disconnectFromServer()

    server.newConnection.connect(accept)
    print("ready", flush=True)
    return qt.exec()


if __name__ == "__main__":
    sys.exit(main())
