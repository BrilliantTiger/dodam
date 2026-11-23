// Copy a file, send it over a local socket, copy one connection into another and serve it over HTTP,
// each through the library call a Node.js program would use.
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import express from "express";

const mode = process.argv[2];

function drain(from: net.Socket, file: string, done: () => void): void {
  const out = fs.createWriteStream(file);
  from.pipe(out);
  out.on("finish", done);
}

if (mode === "temp") {
  // a temporary file, made where the runtime makes one
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), "temp-"));
  fs.writeFileSync(path.join(folder, "file"), "temp");
  if (fs.readFileSync(path.join(folder, "file"), "utf8") !== "temp") process.exit(1);
  console.log(fs.realpathSync(path.dirname(folder)));
} else if (mode === "copy") {
  fs.copyFileSync("in.bin", "out.bin");
  console.log("ok");
} else if (mode === "send") {
  const server = net.createServer((c) => drain(c, "got.bin", () => { console.log("ok"); server.close(); }));
  server.listen("sock", () => {
    const c = net.connect("sock", () => fs.createReadStream("in.bin").pipe(c));
  });
} else if (mode === "relay") {
  const sink = net.createServer((c) => drain(c, "got.bin", () => { console.log("ok"); sink.close(); relay.close(); }));
  const relay = net.createServer((c) => {
    const out = net.connect("sink", () => c.pipe(out));
  });
  sink.listen("sink", () => relay.listen("relay", () => {
    const c = net.connect("relay", () => fs.createReadStream("in.bin").pipe(c));
  }));
} else {
  const app = express();
  app.get("/in.bin", (_req, res) => { res.sendFile(path.resolve("in.bin")); });
  app.get("/quit", (_req, res) => { res.send("bye"); setTimeout(() => process.exit(0), 300); });
  app.listen(18080, "127.0.0.1");
}
