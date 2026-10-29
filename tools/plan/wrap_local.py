import sys, pathlib
src = pathlib.Path(sys.argv[1]); dst = pathlib.Path(sys.argv[2])
body = src.read_text()
dst.write_text('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"></head><body>\n' + body + '\n</body></html>\n')
