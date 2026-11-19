"""Copy a file, send it over a local socket, copy one connection into another and serve it over HTTP,
each through the library call a Python program would use."""
import os
import shutil
import socket
import sys
import threading

mode = sys.argv[1]


def drain(sock, path):
    with open(path, "wb") as out:
        while True:
            data = sock.recv(65536)
            if not data:
                break
            out.write(data)


if mode == "copy":
    shutil.copyfile("in.bin", "out.bin")
elif mode == "send":
    a, b = socket.socketpair()
    taking = threading.Thread(target=drain, args=(b, "got.bin"))
    taking.start()
    with open("in.bin", "rb") as source:
        a.sendfile(source)
    a.close()
    taking.join()
elif mode == "relay":
    a1, a2 = socket.socketpair()
    b1, b2 = socket.socketpair()
    taking = threading.Thread(target=drain, args=(b2, "got.bin"))
    taking.start()

    def feed():
        with open("in.bin", "rb") as source:
            a1.sendall(source.read())
        a1.close()

    threading.Thread(target=feed).start()
    with a2.makefile("rb") as source, b1.makefile("wb") as target:
        shutil.copyfileobj(source, target)
    b1.close()
    taking.join()
elif mode == "serve":
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import FileResponse

    app = FastAPI()

    @app.get("/in.bin")
    def file():
        return FileResponse("in.bin")

    @app.get("/quit")
    def quit():
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"bye": True}

    uvicorn.run(app, host="127.0.0.1", port=18080, log_level="error")
print("ok")
