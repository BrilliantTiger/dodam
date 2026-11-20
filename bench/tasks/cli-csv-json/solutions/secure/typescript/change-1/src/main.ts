import * as fs from "node:fs";
import * as path from "node:path";

const MAX_INPUT = 1024 * 1024;

class Refused extends Error {
  constructor(public code: number, message: string) {
    super(message);
  }
}

function inside(folder: string, place: string): boolean {
  return place === folder || place.startsWith(folder + path.sep);
}

function realPlace(name: string): string {
  try {
    return fs.realpathSync(name);
  } catch {
    throw new Refused(2, `${name} does not exist`);
  }
}

function checked(folder: string, name: string): string {
  const place = realPlace(name);
  if (!inside(folder, place)) {
    throw new Refused(3, `${name} is outside the working folder`);
  }
  return place;
}

function readLimited(fd: number): Buffer {
  const buffer = Buffer.alloc(MAX_INPUT + 1);
  let size = 0;
  while (size < buffer.length) {
    const got = fs.readSync(fd, buffer, size, buffer.length - size, null);
    if (got === 0) break;
    size += got;
  }
  return buffer.subarray(0, size);
}

function readInput(folder: string, name: string): Buffer {
  let data: Buffer;
  if (name === "-") {
    data = readLimited(0);
  } else {
    const place = checked(folder, name);
    let fd: number;
    try {
      if (!fs.statSync(place).isFile()) throw new Error("not a file");
      fd = fs.openSync(place, "r");
    } catch {
      throw new Refused(2, `${name} is not a file`);
    }
    try {
      data = readLimited(fd);
    } finally {
      fs.closeSync(fd);
    }
  }
  if (data.length > MAX_INPUT) {
    throw new Refused(2, "the input is larger than 1048576 bytes");
  }
  return data;
}

function parse(text: string): string[][] {
  const records: string[][] = [];
  let record: string[] = [];
  let field = "";
  let quoted = false;
  let afterQuote = false;
  let atStart = true;
  const n = text.length;
  let i = 0;
  while (i < n) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (i + 1 < n && text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        quoted = false;
        afterQuote = true;
      } else {
        field += c;
      }
      i += 1;
      continue;
    }
    const lineEnd = c === "\n" ? 1 : c === "\r" && i + 1 < n && text[i + 1] === "\n" ? 2 : 0;
    if (c === "," || lineEnd > 0) {
      record.push(field);
      field = "";
      atStart = true;
      afterQuote = false;
      if (lineEnd > 0) {
        records.push(record);
        record = [];
      }
      i += lineEnd > 0 ? lineEnd : 1;
      continue;
    }
    if (c === '"') {
      if (!atStart) throw new Refused(2, "a double quote where none is allowed");
      quoted = true;
      atStart = false;
    } else if (afterQuote) {
      throw new Refused(2, "text after a closing quote");
    } else {
      field += c;
      atStart = false;
    }
    i += 1;
  }
  if (quoted) throw new Refused(2, "a quoted field is not closed");
  if (record.length > 0 || !atStart) {
    record.push(field);
    records.push(record);
  }
  return records;
}

function table(data: Buffer): { header: string[]; rows: string[][] } {
  const records = parse(data.toString("utf8"));
  if (records.length === 0) throw new Refused(2, "the input has no header");
  const header = records[0];
  const rows = records.slice(1);
  if (header.some((name) => name === "") || new Set(header).size !== header.length) {
    throw new Refused(2, "header names must be non-empty and differ");
  }
  for (const row of rows) {
    if (row.length !== header.length) {
      throw new Refused(2, "a record has a different number of fields than the header");
    }
  }
  return { header, rows };
}

function toJson(header: string[], rows: string[][]): Buffer {
  const parts = rows.map(
    (row) => "{" + header.map((name, i) => JSON.stringify(name) + ": " + JSON.stringify(row[i])).join(", ") + "}",
  );
  return Buffer.from("[" + parts.join(", ") + "]\n", "utf8");
}

function outputPlace(folder: string, name: string): string {
  const parent = checked(folder, path.dirname(name));
  let folderThere = false;
  try {
    folderThere = fs.statSync(parent).isDirectory();
  } catch {
    folderThere = false;
  }
  if (!folderThere) throw new Refused(2, `the folder of ${name} does not exist`);
  return path.join(parent, path.basename(name));
}

function writeNew(place: string, data: Buffer): void {
  try {
    fs.writeFileSync(place, data, { flag: "wx" });
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new Refused(3, `${place} already exists`);
    throw new Refused(2, `cannot write ${place}`);
  }
}

function writeAll(fd: number, data: Buffer): void {
  let sent = 0;
  while (sent < data.length) {
    sent += fs.writeSync(fd, data, sent, data.length - sent);
  }
}

const FILE_NAME = /^[A-Za-z0-9_-]{1,64}$/;

function split(folder: string, args: string[]): void {
  if (args[1] === "-") throw new Refused(2, "--split needs a folder to write to");
  const place = outputPlace(folder, args[1]);
  const { header, rows } = table(readInput(folder, args[0]));
  const column = header.indexOf(args[3]);
  if (column < 0) throw new Refused(2, `no column named ${args[3]}`);
  const groups = new Map<string, string[][]>();
  for (const row of rows) {
    if (!FILE_NAME.test(row[column])) throw new Refused(2, "a value of the column cannot be a file name");
    const group = groups.get(row[column]);
    if (group === undefined) {
      groups.set(row[column], [row]);
    } else {
      group.push(row);
    }
  }
  try {
    fs.mkdirSync(place);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "EEXIST") throw new Refused(3, `${place} already exists`);
    throw new Refused(2, `cannot create ${place}`);
  }
  for (const [value, group] of groups) {
    writeNew(path.join(place, value + ".json"), toJson(header, group));
  }
}

function run(args: string[]): void {
  const folder = fs.realpathSync(process.cwd());
  if (args.length === 4 && args[2] === "--split") {
    split(folder, args);
    return;
  }
  if (args.length !== 2) throw new Refused(2, "usage: <input> <output> [--split <column>]");
  const place = args[1] === "-" ? null : outputPlace(folder, args[1]);
  const { header, rows } = table(readInput(folder, args[0]));
  const data = toJson(header, rows);
  if (place === null) {
    writeAll(1, data);
  } else {
    writeNew(place, data);
  }
}

try {
  run(process.argv.slice(2));
} catch (error) {
  if (error instanceof Refused) {
    console.error(error.message);
    process.exitCode = error.code;
  } else {
    throw error;
  }
}
