import json
import os
import re
import sys

MAX_INPUT = 1024 * 1024
FILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def inside(folder: str, place: str) -> bool:
    return place == folder or place.startswith(folder + os.sep)


def real_place(path: str) -> str:
    return os.path.realpath(path)


def checked(folder: str, path: str) -> str:
    place = real_place(path)
    if not inside(folder, place):
        raise Refused(3, f"{path} is outside the working folder")
    return place


def read_input(folder: str, name: str) -> bytes:
    if name == "-":
        data = sys.stdin.buffer.read(MAX_INPUT + 1)
    else:
        place = checked(folder, name)
        if not os.path.isfile(place):
            raise Refused(2, f"{name} is not a file")
        with open(place, "rb") as f:
            data = f.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise Refused(2, "the input is larger than 1048576 bytes")
    return data


def parse(text: str) -> list[list[str]]:
    records: list[list[str]] = []
    record: list[str] = []
    field: list[str] = []
    quoted = after_quote = False
    at_start = True
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quoted:
            if c == '"':
                if i + 1 < n and text[i + 1] == '"':
                    field.append('"')
                    i += 2
                    continue
                quoted, after_quote = False, True
            else:
                field.append(c)
            i += 1
            continue
        line_end = 1 if c == "\n" else 2 if c == "\r" and i + 1 < n and text[i + 1] == "\n" else 0
        if c == "," or line_end:
            record.append("".join(field))
            field = []
            at_start, after_quote = True, False
            if line_end:
                records.append(record)
                record = []
            i += line_end or 1
            continue
        if c == '"':
            if not at_start:
                raise Refused(2, "a double quote where none is allowed")
            quoted, at_start = True, False
        elif after_quote:
            raise Refused(2, "text after a closing quote")
        else:
            field.append(c)
            at_start = False
        i += 1
    if quoted:
        raise Refused(2, "a quoted field is not closed")
    if record or not at_start:
        record.append("".join(field))
        records.append(record)
    return records


def table(data: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        records = parse(data.decode("utf-8"))
    except UnicodeDecodeError:
        raise Refused(2, "the input is not UTF-8") from None
    if not records:
        raise Refused(2, "the input has no header")
    header, rows = records[0], records[1:]
    if any(name == "" for name in header) or len(set(header)) != len(header):
        raise Refused(2, "header names must be non-empty and differ")
    for row in rows:
        if len(row) != len(header):
            raise Refused(2, "a record has a different number of fields than the header")
    return header, rows


def to_json(header: list[str], rows: list[list[str]]) -> bytes:
    return (json.dumps([dict(zip(header, row)) for row in rows], ensure_ascii=False) + "\n").encode("utf-8")


def output_place(folder: str, name: str) -> str:
    parent = checked(folder, os.path.dirname(os.path.abspath(name)))
    if not os.path.isdir(parent):
        raise Refused(2, f"the folder of {name} does not exist")
    return os.path.join(parent, os.path.basename(name))


def write_new(place: str, data: bytes) -> None:
    try:
        with open(place, "xb") as f:
            f.write(data)
    except FileExistsError:
        raise Refused(3, f"{place} already exists") from None


def split(folder: str, args: list[str]) -> None:
    if args[1] == "-":
        raise Refused(2, "--split needs a folder to write to")
    place = output_place(folder, args[1])
    header, rows = table(read_input(folder, args[0]))
    if args[3] not in header:
        raise Refused(2, f"no column named {args[3]}")
    column = header.index(args[3])
    groups: dict[str, list[list[str]]] = {}
    for row in rows:
        if not FILE_NAME.fullmatch(row[column]):
            raise Refused(2, "a value of the column cannot be a file name")
        groups.setdefault(row[column], []).append(row)
    try:
        os.mkdir(place)
    except FileExistsError:
        raise Refused(3, f"{place} already exists") from None
    for value, group in groups.items():
        write_new(os.path.join(place, value + ".json"), to_json(header, group))


def run(args: list[str]) -> None:
    folder = os.path.realpath(os.getcwd())
    if len(args) == 4 and args[2] == "--split":
        return split(folder, args)
    if len(args) != 2:
        raise Refused(2, "usage: <input> <output> [--split <column>]")
    place = None if args[1] == "-" else output_place(folder, args[1])
    header, rows = table(read_input(folder, args[0]))
    data = to_json(header, rows)
    if place is None:
        sys.stdout.buffer.write(data)
    else:
        write_new(place, data)


def main() -> int:
    try:
        run(sys.argv[1:])
    except Refused as refused:
        print(refused, file=sys.stderr)
        return refused.code
    return 0


if __name__ == "__main__":
    sys.exit(main())
